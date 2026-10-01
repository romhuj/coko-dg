package org.coyote.mobile;

import android.content.Context;
import android.net.Uri;
import android.os.SystemClock;
import com.k2fsa.sherpa.onnx.OfflineTts;
import com.k2fsa.sherpa.onnx.OfflineTtsConfig;
import com.k2fsa.sherpa.onnx.OfflineTtsKokoroModelConfig;
import com.k2fsa.sherpa.onnx.OfflineTtsModelConfig;
import com.k2fsa.sherpa.onnx.OfflineTtsVitsModelConfig;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.List;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Fixed public model revisions. Only manifest-listed individual files can be installed. */
final class TtsModel {
    interface Progress { void update(String message); }
    private static final ReentrantLock INSTALL_LOCK=new ReentrantLock();
    private final Context context;
    private final List<VoiceModel.Spec> files=new ArrayList<>();
    final File directory;
    final String voiceId, pack, revision;
    final int speakerId;
    private final long totalBytes;
    private final VoiceModel verifier;

    TtsModel(Context context,String voiceId) throws IOException { this(context,voiceId,null); }

    /** The optional root is for opt-in fixture tests; it never changes production paths. */
    TtsModel(Context context,String voiceId,File root) throws IOException {
        this.context=context.getApplicationContext(); this.voiceId=voiceId;
        if("kokoro-zf_001".equals(voiceId)) { pack="kokoro-v1_1"; speakerId=3; }
        else if("kokoro-zm_010".equals(voiceId)) { pack="kokoro-v1_1"; speakerId=59; }
        else if("melo-zh".equals(voiceId)) { pack="melo-zh"; speakerId=0; }
        else throw new IOException("未找到所选离线音色");
        try(InputStream input=this.context.getAssets().open("tts/"+pack+".json")) {
            ByteArrayOutputStream bytes=new ByteArrayOutputStream(); byte[] buffer=new byte[8192]; int count;
            while((count=input.read(buffer))!=-1) {
                if(bytes.size()+count>256*1024) throw new IOException("音色资源清单过大");
                bytes.write(buffer,0,count);
            }
            JSONObject manifest=new JSONObject(bytes.toString(StandardCharsets.UTF_8.name()));
            if(manifest.getInt("schema")!=1 || !pack.equals(manifest.getString("id")))
                throw new IOException("音色资源清单版本不符");
            revision=manifest.getString("revision");
            String repository=manifest.getString("repository");
            if(!revision.matches("[a-f0-9]{40}") || !repository.matches("csukuangfj/[A-Za-z0-9_-]+"))
                throw new IOException("音色资源清单地址无效");
            directory=new File(root==null ? new File(this.context.getNoBackupFilesDir(),"tts") : root,
                    pack+"-"+revision.substring(0,8));
            JSONArray entries=manifest.getJSONArray("files"); long total=0;
            for(int i=0;i<entries.length();i++) {
                JSONObject entry=entries.getJSONObject(i); String path=entry.getString("path");
                long size=entry.getLong("bytes"); String sha=entry.getString("sha256");
                if(path.startsWith("/") || path.contains("..") || !path.matches("[A-Za-z0-9_./!+ -]+")
                        || size<=0 || !sha.matches("[a-f0-9]{64}")) throw new IOException("音色资源清单文件无效");
                files.add(new VoiceModel.Spec(path,"https://huggingface.co/"+repository+"/resolve/"+revision+"/"+Uri.encode(path,"/"),size,sha));
                total+=size;
            }
            if(files.isEmpty() || total!=manifest.getLong("total_bytes")) throw new IOException("音色资源清单大小不符");
            totalBytes=total;
            verifier=new VoiceModel(directory,this.context.getAssets());
        } catch(JSONException invalid) { throw new IOException("音色资源清单无效",invalid); }
    }

    void prepare(VoiceModel.CancelToken token,Progress progress,boolean allowDownload) throws IOException {
        try {
            while(true) { token.check(); if(INSTALL_LOCK.tryLock(100,TimeUnit.MILLISECONDS)) break; }
        } catch(InterruptedException cancelled) {
            Thread.currentThread().interrupt(); throw new InterruptedIOException("音色准备已取消");
        }
        try {
            token.check();
            if(!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法创建离线音色目录");
            long completed=0; boolean unpackAttempted=false;
            for(VoiceModel.Spec spec:files) {
                token.check(); File destination=resolve(spec.filename);
                progress.update("正在校验离线音色 "+percent(completed)+"%");
                if(!verifier.verifiedCached(destination,spec,token)) {
                    if(!allowDownload) throw new IOException("离线音色资源缺失或校验不符："+spec.filename);
                    if(!large(spec) && !unpackAttempted) {
                        unpackAttempted=true;
                        installBundled(token,progress);
                        if(verifier.verifiedCached(destination,spec,token)) {completed+=spec.bytes;continue;}
                    }
                    File parent=destination.getParentFile();
                    if(!parent.isDirectory() && !parent.mkdirs()) throw new IOException("无法创建离线音色目录");
                    if(destination.exists() && !destination.delete()) throw new IOException("无法更新离线音色文件");
                    download(spec,destination,completed,token,progress);
                    // Reuse the stat/ctime/inode-bound durable receipt implementation. The first
                    // install reads the saved bytes once; future starts only inspect metadata.
                    if(!verifier.verifiedCached(destination,spec,token)) {
                        destination.delete(); throw new IOException("离线音色文件校验失败，请重试");
                    }
                }
                completed+=spec.bytes;
            }
            token.check(); progress.update("离线音色已校验，正在加载");
        } finally { INSTALL_LOCK.unlock(); }
    }

    File resolve(String path) throws IOException {
        File file=new File(directory,path);
        if(!file.getCanonicalPath().startsWith(directory.getCanonicalPath()+File.separator))
            throw new IOException("离线音色路径无效");
        return file;
    }

    private String path(String name) throws IOException { return resolve(name).getAbsolutePath(); }
    private int percent(long bytes) { return (int)Math.min(100,bytes*100/totalBytes); }

    private static boolean large(VoiceModel.Spec spec) {
        return "model.int8.onnx".equals(spec.filename)||"voices.bin".equals(spec.filename);
    }

    /** Small phonemizer files travel in one APK asset; only large neural weights need HTTP. */
    void installBundled(VoiceModel.CancelToken token,Progress progress) throws IOException {
        token.check();
        InputStream source;
        try {source=context.getAssets().open("tts/"+pack+".zip");}
        catch(FileNotFoundException absent) {return;}
        Map<String,VoiceModel.Spec> allowed=new HashMap<>();
        for(VoiceModel.Spec spec:files) if(!large(spec)) allowed.put(spec.filename,spec);
        Set<String> seen=new HashSet<>(); int installed=0;
        try(ZipInputStream archive=new ZipInputStream(source)) {
            ZipEntry entry;
            while((entry=archive.getNextEntry())!=null) {
                token.check(); String name=entry.getName();
                if(entry.isDirectory()) continue;
                VoiceModel.Spec spec=allowed.get(name);
                if(spec==null||!seen.add(name)) throw new IOException("离线音色内置资源包含无效文件");
                File destination=resolve(name);
                if(verifier.verifiedCached(destination,spec,token)) {archive.closeEntry();continue;}
                File parent=destination.getParentFile();
                if(!parent.isDirectory()&&!parent.mkdirs()) throw new IOException("无法创建离线音色目录");
                File temporary=new File(destination.getPath()+".bundled.part");
                try {
                    MessageDigest hash;
                    try {hash=MessageDigest.getInstance("SHA-256");}
                    catch(NoSuchAlgorithmException impossible) {throw new IOException(impossible);}
                    long copied=0; byte[] buffer=new byte[65536];
                    try(FileOutputStream output=new FileOutputStream(temporary)) {
                        int count;
                        while((count=archive.read(buffer))!=-1) {
                            token.check(); copied+=count;
                            if(copied>spec.bytes) throw new IOException("离线音色内置资源大小不符");
                            hash.update(buffer,0,count);output.write(buffer,0,count);
                        }
                        output.getFD().sync();
                    }
                    if(copied!=spec.bytes||!VoiceModel.hex(hash.digest()).equals(spec.sha256))
                        throw new IOException("离线音色内置资源校验失败");
                    token.check();publish(temporary,destination);
                    if(!verifier.verifiedCached(destination,spec,token)) throw new IOException("离线音色内置资源保存失败");
                } finally {if(temporary.exists())temporary.delete();}
                installed++;
                progress.update("正在准备离线音色资源 "+installed+"/"+allowed.size());
                archive.closeEntry();
            }
        }
        if(seen.size()!=allowed.size()) throw new IOException("离线音色内置资源不完整");
    }

    OfflineTts create() throws IOException {
        OfflineTtsModelConfig models=new OfflineTtsModelConfig();
        models.setNumThreads(Math.min(2,Math.max(1,Runtime.getRuntime().availableProcessors()/2)));
        models.setProvider("cpu"); models.setDebug(false);
        OfflineTtsConfig config=new OfflineTtsConfig(); config.setMaxNumSentences(1);
        if(pack.equals("kokoro-v1_1")) {
            OfflineTtsKokoroModelConfig kokoro=new OfflineTtsKokoroModelConfig();
            kokoro.setModel(path("model.int8.onnx")); kokoro.setVoices(path("voices.bin"));
            kokoro.setTokens(path("tokens.txt")); kokoro.setDataDir(path("espeak-ng-data"));
            kokoro.setLexicon(path("lexicon-us-en.txt")+","+path("lexicon-zh.txt"));
            models.setKokoro(kokoro);
            config.setRuleFsts(path("phone-zh.fst")+","+path("date-zh.fst")+","+path("number-zh.fst"));
        } else {
            OfflineTtsVitsModelConfig vits=new OfflineTtsVitsModelConfig();
            vits.setModel(path("model.int8.onnx")); vits.setTokens(path("tokens.txt"));
            vits.setLexicon(path("lexicon.txt")); models.setVits(vits);
            config.setRuleFsts(path("phone.fst")+","+path("date.fst")+","+path("number.fst")+","+path("new_heteronym.fst"));
        }
        config.setModel(models); return new OfflineTts(null,config);
    }

    private void download(VoiceModel.Spec spec,File destination,long completed,VoiceModel.CancelToken token,Progress progress) throws IOException {
        try { downloadFrom(spec,spec.url,destination,completed,token,progress); }
        catch(IOException first) {
            token.check();
            downloadFrom(spec,spec.url.replace("https://huggingface.co/","https://hf-mirror.com/"),
                    destination,completed,token,progress);
        }
    }

    private void downloadFrom(VoiceModel.Spec spec,String url,File destination,long completed,
                              VoiceModel.CancelToken token,Progress progress) throws IOException {
        File part=new File(destination.getPath()+".part");
        long offset=part.isFile()?part.length():0;
        if(offset>spec.bytes || offset==spec.bytes && !VoiceModel.verified(part,spec,token)) {
            if(!part.delete()) throw new IOException("无法清理未完成的音色文件"); offset=0;
        }
        if(offset==spec.bytes) { publish(part,destination); return; }
        if(directory.getUsableSpace()<spec.bytes-offset+32L*1024*1024)
            throw new IOException("存储空间不足，所选离线音色需要约"+((totalBytes+999999)/1000000)+"MB");
        progress.update("正在下载离线音色 "+percent(completed+offset)+"%");
        HttpURLConnection connection=open(url,offset,token);
        try {
            int status=connection.getResponseCode();
            if(status!=200 && status!=206) throw new IOException("离线音色下载失败（HTTP "+status+"），请重试");
            if(status==206) {
                String range=connection.getHeaderField("Content-Range");
                if(range==null || !range.startsWith("bytes "+offset+"-") || !range.endsWith("/"+spec.bytes))
                    throw new IOException("离线音色下载分段无效");
            } else offset=0;
            MessageDigest hash;
            try { hash=MessageDigest.getInstance("SHA-256"); }
            catch(NoSuchAlgorithmException impossible) { throw new IOException(impossible); }
            byte[] buffer=new byte[65536];
            if(offset>0) try(InputStream prior=new FileInputStream(part)) {
                int count; long read=0;
                while((count=prior.read(buffer))!=-1) { token.check(); hash.update(buffer,0,count); read+=count; }
                if(read!=offset) throw new IOException("离线音色下载分段已变化");
            }
            long received=offset,lastProgress=0;
            try(InputStream input=connection.getInputStream();FileOutputStream output=new FileOutputStream(part,offset>0)) {
                int count;
                while((count=input.read(buffer))!=-1) {
                    token.check(); received+=count;
                    if(received>spec.bytes) throw new IOException("离线音色下载大小不符");
                    output.write(buffer,0,count); hash.update(buffer,0,count);
                    long now=SystemClock.elapsedRealtime();
                    if(now-lastProgress>=500) { lastProgress=now; progress.update("正在下载离线音色 "+percent(completed+received)+"%"); }
                }
                output.getFD().sync();
            }
            token.check();
            if(received!=spec.bytes) throw new IOException("离线音色下载尚未完成，请重试继续下载");
            if(!VoiceModel.hex(hash.digest()).equals(spec.sha256)) {
                part.delete(); throw new IOException("离线音色下载校验失败，请重试");
            }
            publish(part,destination);
        } finally { token.detach(connection); connection.disconnect(); }
    }

    private static void publish(File part,File destination) throws IOException {
        if(!part.renameTo(destination)) throw new IOException("无法保存已校验的离线音色");
    }

    private static HttpURLConnection open(String source,long offset,VoiceModel.CancelToken token) throws IOException {
        URL url=new URL(source);
        for(int redirect=0;redirect<6;redirect++) {
            token.check();
            if(!"https".equalsIgnoreCase(url.getProtocol())) throw new IOException("音色下载必须使用HTTPS");
            HttpURLConnection connection=(HttpURLConnection)url.openConnection();
            connection.setInstanceFollowRedirects(false); connection.setConnectTimeout(10000); connection.setReadTimeout(15000);
            connection.setRequestProperty("Accept-Encoding","identity");
            if(offset>0) connection.setRequestProperty("Range","bytes="+offset+"-");
            token.attach(connection);
            try {
                int status=connection.getResponseCode();
                if(status!=301 && status!=302 && status!=303 && status!=307 && status!=308) return connection;
                String target=connection.getHeaderField("Location");
                if(target==null) throw new IOException("离线音色下载地址无效");
                url=new URL(url,target);
            } catch(IOException error) { token.detach(connection);connection.disconnect();throw error; }
            token.detach(connection);connection.disconnect();
        }
        throw new IOException("离线音色下载重定向过多");
    }
}
