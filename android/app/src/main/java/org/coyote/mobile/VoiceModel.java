package org.coyote.mobile;

import android.content.Context;
import android.content.res.AssetManager;
import android.os.Build;
import android.os.SystemClock;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.system.StructStat;
import android.util.AtomicFile;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;

/** Pinned, individually verified model files. No archive extraction or user credentials. */
final class VoiceModel {
    static final String MODEL_VERSION = "sensevoice-2024-07-17-int8";
    private static final String HF = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/2365baeacb507f821a0c8120fcee3d484dba7a07/";
    static final Spec[] FILES = {
        new Spec("model.int8.onnx", HF + "model.int8.onnx", 239233841L,
                "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"),
        new Spec("tokens.txt", HF + "tokens.txt", 315894L,
                "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"),
        new Spec("silero_vad.onnx", "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx", 643854L,
                "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"),
    };
    static final long TOTAL_BYTES = 240193589L;
    private final File directory;
    private final AssetManager assets;
    private static final ReentrantLock FILE_LOCK=new ReentrantLock();

    static final class PreparationStats {
        long elapsedMs, checkMs, downloadMs, hashedBytes, downloadedBytes;
        int cacheHits, hashedFiles;
        JSONObject json() {
            JSONObject result=new JSONObject();
            try {
                result.put("elapsed_ms",elapsedMs); result.put("check_ms",checkMs);
                result.put("download_ms",downloadMs); result.put("hashed_bytes",hashedBytes);
                result.put("downloaded_bytes",downloadedBytes); result.put("cache_hits",cacheHits);
                result.put("hashed_files",hashedFiles);
            } catch(org.json.JSONException impossible) { throw new IllegalStateException(impossible); }
            return result;
        }
    }

    /** Nanosecond change times detect same-size writes even if the caller restores mtime. */
    private static final class Stamp {
        final String identity;
        final long size;
        Stamp(StructStat stat) {
            size=stat.st_size;
            identity=stat.st_dev+":"+stat.st_ino+":"+stat.st_mode+":"+stat.st_uid+":"+stat.st_gid
                    +":"+stat.st_nlink+":"+stat.st_size+":"+stat.st_mtime+":"+stat.st_ctime
                    +(Build.VERSION.SDK_INT>=27 ? ":"+stat.st_mtim.tv_nsec+":"+stat.st_ctim.tv_nsec : "");
        }
        boolean same(Stamp other) { return other!=null && identity.equals(other.identity); }
    }

    interface Progress { void update(int percent, boolean downloading); }

    static final class Spec {
        final String filename, url, sha256;
        final long bytes;
        Spec(String filename, String url, long bytes, String sha256) {
            this.filename=filename; this.url=url; this.bytes=bytes; this.sha256=sha256;
        }
    }

    static final class CancelToken {
        private volatile boolean cancelled;
        private volatile HttpURLConnection connection;
        boolean isCancelled() { return cancelled || Thread.currentThread().isInterrupted(); }
        void check() throws InterruptedIOException {
            if (isCancelled()) throw new InterruptedIOException("语音准备已取消");
        }
        synchronized void attach(HttpURLConnection value) throws InterruptedIOException {
            check(); connection=value;
        }
        synchronized void detach(HttpURLConnection value) {
            if (connection == value) connection=null;
        }
        void cancel() {
            HttpURLConnection active;
            synchronized (this) { cancelled=true; active=connection; connection=null; }
            if (active != null) {
                Thread disconnect=new Thread(active::disconnect, "CoyoteVoiceDownloadCancel");
                disconnect.setDaemon(true); disconnect.start();
            }
        }
    }

    VoiceModel(Context context) {
        this(new File(context.getNoBackupFilesDir(), "voice/" + MODEL_VERSION),context.getAssets());
    }
    VoiceModel(File directory, AssetManager assets) { this.directory=directory; this.assets=assets; }
    File model() { return new File(directory, FILES[0].filename); }
    File tokens() { return new File(directory, FILES[1].filename); }
    File vad() { return new File(directory, FILES[2].filename); }

    PreparationStats prepare(CancelToken token, Progress progress) throws IOException {
        lock(token);
        try { return prepareLocked(token,progress,true); }
        finally { FILE_LOCK.unlock(); }
    }

    /** Offline preflight only: never creates/downloads a missing model or initializes JNI. */
    PreparationStats preverifyCached(CancelToken token) throws IOException {
        lock(token);
        try { return prepareLocked(token,(percent,downloading)->{},false); }
        finally { FILE_LOCK.unlock(); }
    }

    private PreparationStats prepareLocked(CancelToken token, Progress progress, boolean install) throws IOException {
        long started=SystemClock.elapsedRealtime();
        PreparationStats stats=new PreparationStats();
        token.check();
        if (!install && !directory.isDirectory()) return stats;
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法创建语音模型目录");
        long completed=0;
        for (Spec spec : FILES) {
            token.check();
            File destination=new File(directory, spec.filename);
            progress.update(percent(completed), false);
            long checking=SystemClock.elapsedRealtime();
            boolean ready=verifiedCachedLocked(destination,spec,token,stats);
            stats.checkMs+=SystemClock.elapsedRealtime()-checking;
            if (!ready && install) {
                if (destination.exists() && !destination.delete()) throw new IOException("无法更新语音模型文件");
                long downloading=SystemClock.elapsedRealtime();
                if (!installBundled(spec, destination, token))
                    download(spec, destination, completed, token, progress,stats);
                stats.downloadMs+=SystemClock.elapsedRealtime()-downloading;
            }
            completed+=spec.bytes;
            progress.update(percent(completed), false);
        }
        stats.elapsedMs=SystemClock.elapsedRealtime()-started;
        return stats;
    }

    private static void lock(CancelToken token) throws IOException {
        try { while(true) { token.check(); if(FILE_LOCK.tryLock(100,TimeUnit.MILLISECONDS)) return; } }
        catch(InterruptedException interrupted) { Thread.currentThread().interrupt(); throw new InterruptedIOException("语音准备已取消"); }
    }

    private static Stamp stamp(File file) {
        try {
            StructStat stat=Os.lstat(file.getAbsolutePath());
            return OsConstants.S_ISREG(stat.st_mode) ? new Stamp(stat) : null;
        } catch(ErrnoException missing) { return null; }
    }

    private AtomicFile receipt(Spec spec) { return new AtomicFile(new File(directory,spec.filename+".verified.json")); }

    boolean cachedOnly(File file,Spec spec) {
        // Older Android exposes only second-resolution ctime. Do not weaken cache identity there.
        if(Build.VERSION.SDK_INT<27) return false;
        Stamp before=stamp(file);
        if(before==null || before.size!=spec.bytes) return false;
        AtomicFile receipt=receipt(spec);
        if(receipt.getBaseFile().length()>4096) return false;
        try(FileInputStream input=receipt.openRead()) {
            byte[] bytes=new byte[4097]; int total=0,count;
            while(total<bytes.length && (count=input.read(bytes,total,bytes.length-total))!=-1) total+=count;
            if(total>4096) return false;
            JSONObject saved=new JSONObject(new String(bytes,0,total,StandardCharsets.UTF_8));
            return saved.optInt("schema")==1 && MODEL_VERSION.equals(saved.optString("version"))
                    && spec.sha256.equals(saved.optString("sha256")) && spec.bytes==saved.optLong("bytes",-1)
                    && file.getAbsolutePath().equals(saved.optString("path"))
                    && before.identity.equals(saved.optString("stat")) && before.same(stamp(file));
        } catch(IOException | org.json.JSONException invalid) { return false; }
    }

    boolean verifiedCached(File file,Spec spec,CancelToken token) throws IOException {
        lock(token);
        try { return verifiedCachedLocked(file,spec,token,new PreparationStats()); }
        finally { FILE_LOCK.unlock(); }
    }

    private boolean verifiedCachedLocked(File file,Spec spec,CancelToken token,PreparationStats stats) throws IOException {
        token.check();
        if(cachedOnly(file,spec)) { token.check(); stats.cacheHits++; return true; }
        receipt(spec).delete();
        Stamp verified=hashFile(file,spec,token,stats);
        if(verified==null) return false;
        remember(file,spec,verified,token);
        return true;
    }

    private void remember(File file,Spec spec,Stamp verified,CancelToken token) throws IOException {
        token.check();
        if(verified==null || verified.size!=spec.bytes || !verified.same(stamp(file))) throw new IOException("语音模型在校验期间发生变化，请重试");
        if(Build.VERSION.SDK_INT<27) return;
        AtomicFile receipt=receipt(spec); FileOutputStream output=null;
        try {
            JSONObject value=new JSONObject();
            value.put("schema",1); value.put("version",MODEL_VERSION); value.put("sha256",spec.sha256);
            value.put("bytes",spec.bytes); value.put("path",file.getAbsolutePath()); value.put("stat",verified.identity);
            output=receipt.startWrite(); output.write(value.toString().getBytes(StandardCharsets.UTF_8));
            token.check(); receipt.finishWrite(output);
        } catch(org.json.JSONException impossible) { receipt.failWrite(output); throw new IOException(impossible); }
        catch(IOException failure) { receipt.failWrite(output); throw failure; }
    }

    private static int percent(long bytes) { return (int)Math.min(100, bytes * 100 / TOTAL_BYTES); }

    boolean installBundled(Spec spec, File destination, CancelToken token) throws IOException {
        if (!"silero_vad.onnx".equals(spec.filename)) return false;
        InputStream input;
        try { input=assets.open("voice/silero_vad.onnx"); }
        catch (FileNotFoundException absent) { return false; }
        File temporary=new File(destination.getParentFile(), spec.filename + ".bundled.part");
        try {
            MessageDigest digest=digest();
            long copied=0;
            try (InputStream bundled=input; FileOutputStream output=new FileOutputStream(temporary)) {
                byte[] buffer=new byte[65536];
                int count;
                while ((count=bundled.read(buffer)) != -1) {
                    token.check(); copied+=count;
                    if (copied > spec.bytes) throw new IOException("语音模型备用资源大小不符");
                    output.write(buffer, 0, count);
                    digest.update(buffer,0,count);
                }
                output.getFD().sync();
            }
            token.check();
            if (copied!=spec.bytes || !hex(digest.digest()).equals(spec.sha256)) throw new IOException("语音模型备用资源校验失败");
            token.check();
            if (!temporary.renameTo(destination)) throw new IOException("无法保存已校验的语音模型备用资源");
            if(directory.equals(destination.getParentFile())) remember(destination,spec,stamp(destination),token);
            return true;
        } finally { if (temporary.exists()) temporary.delete(); }
    }

    static boolean verified(File file, Spec spec, CancelToken token) throws IOException {
        return hashFile(file,spec,token,null)!=null;
    }

    private static MessageDigest digest() throws IOException {
        MessageDigest digest;
        try { digest=MessageDigest.getInstance("SHA-256"); }
        catch (NoSuchAlgorithmException e) { throw new IOException("设备不支持模型校验", e); }
        return digest;
    }

    private static Stamp hashFile(File file,Spec spec,CancelToken token,PreparationStats stats) throws IOException {
        token.check();
        Stamp before=stamp(file);
        if(before==null || before.size!=spec.bytes) return null;
        MessageDigest digest=digest();
        byte[] buffer=new byte[65536];
        try (FileInputStream input=new FileInputStream(file)) {
            if(!before.same(new Stamp(Os.fstat(input.getFD())))) return null;
            int count;
            while ((count=input.read(buffer)) != -1) {
                token.check(); digest.update(buffer, 0, count);
                if(stats!=null) stats.hashedBytes+=count;
            }
            if(!before.same(new Stamp(Os.fstat(input.getFD()))) || !before.same(stamp(file))) return null;
        } catch(ErrnoException failure) { throw new IOException("无法读取语音模型状态",failure); }
        if(stats!=null) stats.hashedFiles++;
        token.check();
        return hex(digest.digest()).equals(spec.sha256) ? before : null;
    }

    private static void hashPrefix(File part,long offset,MessageDigest digest,CancelToken token,PreparationStats stats) throws IOException {
        Stamp before=stamp(part);
        if(before==null || before.size!=offset) throw new IOException("语音模型下载分段已变化，请重试");
        byte[] buffer=new byte[65536];
        try(FileInputStream input=new FileInputStream(part)) {
            long read=0; int count;
            while((count=input.read(buffer))!=-1) {
                token.check(); digest.update(buffer,0,count); read+=count; stats.hashedBytes+=count;
            }
            if(read!=offset || !before.same(stamp(part))) throw new IOException("语音模型下载分段已变化，请重试");
        }
    }

    static String hex(byte[] bytes) {
        StringBuilder result=new StringBuilder(bytes.length * 2);
        for (byte value : bytes) result.append(String.format(Locale.ROOT, "%02x", value & 255));
        return result.toString();
    }

    private void download(Spec spec, File destination, long completed, CancelToken token, Progress progress,PreparationStats stats) throws IOException {
        try {
            downloadFrom(spec, spec.url, destination, completed, token, progress,stats);
        } catch (IOException first) {
            token.check();
            if (!spec.url.startsWith("https://huggingface.co/")) throw first;
            // Same pinned revision, exact size and SHA256. No mirror is trusted
            // to change model bytes, and verified partial files are resumed.
            downloadFrom(spec, spec.url.replace("https://huggingface.co/", "https://hf-mirror.com/"),
                    destination, completed, token, progress,stats);
        }
    }

    private void downloadFrom(Spec spec, String source, File destination, long completed, CancelToken token, Progress progress,PreparationStats stats) throws IOException {
        File part=new File(directory, spec.filename + ".part");
        long offset=part.isFile() ? part.length() : 0;
        boolean alreadyVerified=offset==spec.bytes && hashFile(part,spec,token,stats)!=null;
        if (offset > spec.bytes || (offset == spec.bytes && !alreadyVerified)) {
            if (!part.delete()) throw new IOException("无法清理无效模型下载");
            offset=0;
        }
        if (!alreadyVerified) {
            if (directory.getUsableSpace() < spec.bytes - offset + 32L * 1024 * 1024)
                throw new IOException("存储空间不足，语音模型需要约240MB空间");
            HttpURLConnection connection=open(source, offset, token);
            try {
                int status=connection.getResponseCode();
                if (status != HttpURLConnection.HTTP_OK && status != HttpURLConnection.HTTP_PARTIAL)
                    throw new IOException("语音模型下载失败（HTTP " + status + "），请检查网络后重试");
                if (status == HttpURLConnection.HTTP_PARTIAL) {
                    String range=connection.getHeaderField("Content-Range");
                    if (range == null || !range.startsWith("bytes " + offset + "-") || !range.endsWith("/" + spec.bytes))
                        throw new IOException("模型下载分段响应无效，请重试");
                } else offset=0;
                MessageDigest digest=digest();
                // Only a resumed prefix is read. Fresh network bytes are hashed as written.
                if(offset>0) hashPrefix(part,offset,digest,token,stats);
                long received=offset;
                byte[] buffer=new byte[65536];
                int lastPercent=-1;
                try (InputStream input=connection.getInputStream(); FileOutputStream output=new FileOutputStream(part, offset > 0)) {
                    int count;
                    while ((count=input.read(buffer)) != -1) {
                        token.check();
                        received+=count;
                        if (received > spec.bytes) throw new IOException("语音模型文件大小不符");
                        output.write(buffer, 0, count);
                        digest.update(buffer,0,count); stats.downloadedBytes+=count;
                        int current=percent(completed + received);
                        if (current != lastPercent) { progress.update(current, true); lastPercent=current; }
                    }
                    output.getFD().sync();
                }
                if (received != spec.bytes) throw new IOException("语音模型尚未下载完整，请重试继续下载");
                if(!hex(digest.digest()).equals(spec.sha256)) {
                    part.delete(); throw new IOException("语音模型校验失败，请重试下载");
                }
            } finally { token.detach(connection); connection.disconnect(); }
        }
        token.check();
        // Same private directory: rename makes only a completely verified file visible.
        if (!part.renameTo(destination)) throw new IOException("无法保存已校验的语音模型");
        remember(destination,spec,stamp(destination),token);
    }

    private static HttpURLConnection open(String source, long offset, CancelToken token) throws IOException {
        URL url=new URL(source);
        for (int redirects=0; redirects<6; redirects++) {
            token.check();
            if (!"https".equalsIgnoreCase(url.getProtocol())) throw new IOException("模型下载必须使用HTTPS");
            HttpURLConnection connection=(HttpURLConnection)url.openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(10000); connection.setReadTimeout(15000);
            connection.setRequestProperty("Accept-Encoding", "identity");
            if (offset > 0) connection.setRequestProperty("Range", "bytes=" + offset + "-");
            token.attach(connection);
            try {
                int status=connection.getResponseCode();
                if (status != 301 && status != 302 && status != 303 && status != 307 && status != 308) return connection;
                String location=connection.getHeaderField("Location");
                if (location == null) throw new IOException("语音模型下载地址无效");
                url=new URL(url, location);
            } catch (IOException error) {
                token.detach(connection); connection.disconnect(); throw error;
            }
            token.detach(connection); connection.disconnect();
        }
        throw new IOException("语音模型下载重定向过多");
    }
}
