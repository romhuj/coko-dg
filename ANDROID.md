# coko DG · Android 0.7.0

0.7.0 是 [coko DG 公开仓库](https://github.com/romhuj/coko-dg) 的首个正式发行版，Android versionCode 为 1。应用名和菜单标题统一为 coko DG，保留此前的连续朗读、语音输入与聊天存档功能。

本版使用新包名 `io.github.romhuj.cokodg` 与独立正式签名，可与旧 `org.coyote.mobile` 预览版并存，不会覆盖或自动迁移旧版数据。首次安装需要重新配置模型、设备与角色，重新下载所需语音资源。后续正式版更新使用同一包名和签名，保留应用私有数据；卸载则删除数据。

独立 Android APK，在手机/平板内运行 React 界面和 Python 控制逻辑。无需电脑保持运行；模型服务、角色资料搜索以及公共 V4 中继仍需要联网。设备继续由 DG-LAB App 连接，本版不提供直接蓝牙连接。

## 当前范围

- 文本聊天、自动回合及共享情景队列；角色按性格与动机决定是否执行动作。
- 角色页提供默认关闭的「模型自判断」。开启须阅读警示并等待5秒确认，前后端均校验；开启后普通情景措辞由角色判断，固定急停词、息屏停止、急停按钮与A/B上限始终有效。
- 菜单「角色」上方可设置「我的名称」；菜单右上角新建聊天，历史聊天列表长期保存在本机并支持分页。长按一条历史聊天可置顶、取消置顶或删除，置顶状态跨重启保存。更换角色开启新聊天，旧记录保留。
- 网络角色搜索、创建、切换和应用私有存储；创建的角色向左滑动显示置顶和删除，置顶顺序保留。删除当前角色会先停止设备和自动运行，再切回默认角色。
- 「创建角色」可选择搜索资料或自定义创建。自定义角色填写名称、性格及可选背景，保存后直接使用，也可置顶和删除。
- 236 组波形（24 组原有、212 组导入兼容版）与六档强度。
- 独立 A/B 上限、App 上报限制、急停和断线旧动作失效。
- DG-LAB 深色金色风格，主界面切换聊天与设备；角色、波形和设置收进侧边菜单。
- 输入框上方的「自动运行」按钮用颜色显示状态；聊天和自动回合可以同时进行。
- 自动运行旁的麦克风图标开启持续离线语音输入，停顿约0.9秒自动断句并发送；再次点击关闭。语音与文字共用聊天队列、请求编号和角色判断。
- 麦克风旁的音量图标开启回复朗读，包括自动回合；新 AI 回复立即替换旧朗读，不补读历史消息。输入框圆角为12px。
- 聊天页急停提示旁可直接解除；AI 回复下方以独立绿色标签显示已发送的波形、强度和增减操作，数值来自应用上限后的命令。未发送或被拦截的操作单独列为说明。
- 设备页提供 A/B 数字上限输入框、图标式减弱/增强按钮及明确的手动波形播放操作。浏览波形与选择波形不会立即播放。
- 波形请求与同通道强度成组处理，分别显示实际命令回执。缺少强度时保留非零当前值；当前为0时按已有基准及档位使用非零起始强度，最低1且不超过有效上限。明确归零、急停、禁用或上限为0时不会擅自升强。
- 设置「高级」下方新增「关于」，内有「反馈」与「源仓库」，分别在外部浏览器打开 [反馈页面](https://x.com/_Good_Dick_) 和 [本公开仓库](https://github.com/romhuj/coko-dg)。关于页左上角或系统返回键回到设置，急停仍可用。
- 相机、地牢和直接 BLE 入口未在此 Android 版本开放。

侧边菜单可向左滑动、点击右侧空白或按系统返回关闭。设置是独立页面，左上角返回；急停图标在各界面顶部保持可用。正常界面只显示一个网页急停图标；启动、加载失败或 WebView 进程退出时显示原生急停图标作为后备。常驻通知也提供急停与停止服务入口。

## 首次使用

1. 安装 APK，打开「设置」。新安装默认填入 `https://api.deepseek.com` 与 `deepseek-flash`，填写自己的 API Key 后测试并保存。点击「前往获取」会调用用户的外部浏览器打开 [DeepSeek API Key 页面](https://platform.deepseek.com/api_keys)。安装包不包含桌面版密钥。
2. 本地服务启动时处于急停锁定状态，自动运行默认关闭；此时可以文字聊天。
3. 本地服务启动后自动显示与软件同色的设备连接弹窗，提供二维码、取消、复制和「复制并跳转」。取消后本次服务会话不反复弹出，可从设备页重新进入配对。跳转会复制当前链接并打开固定的 DG-LAB4 应用（包名 `com.bjsm.dungeonlabs4`），不会自动声称已连接；未安装时保留复制结果并提示。亮屏切换到 DG-LAB App 时服务会继续运行。在官方 App 中使用 Socket V4；相册识别和链接导入能力需以实机为准。
4. 确认设备已连接、A/B 上限合适，再在设备页显式点击「解除急停」，或点击聊天输入框上方提示旁的「解除」。解除不会重启自动运行或立即输出；自动运行需在聊天输入框上方开启。
5. 设备页展开「手动控制」后可独立操作 A/B 或开启手动联动。选择波形后点击「立即播放」才会发送；「停止并清零」会停止对应通道的波形并归零。顶栏急停会锁定全部设备动作并关闭自动运行。

亮屏后台运行始终启用，没有单独的功能设置，也不会强制屏幕常亮。按系统返回会优先关闭菜单或返回上一级页面；退出聊天主页时可以选择「保持后台」或「停止并退出」。息屏时会请求急停并停止本地服务。网络断开或系统强制终止时，应用无法保证设备已收到停止命令；请以 DG-LAB App 和设备实际状态为准。重新启动不会恢复旧动作队列。

设备卡片显示软件记录值与实际有效上限；上限输入遵循原有后端范围 `1–配置硬上限`，设备输出仍受 DG-LAB App 上报上限约束。六档输出强度不会抬高这些上限。通道上限、六档及联动、手动焦点通道/联动/波形选择会跨重启保存；配件、启用状态、模型设置、昵称和角色继续沿用原有保存文件。重启不会恢复实际输出、自动运行、麦克风或朗读开关。

设备回执区分已确认、已发送、失败及结果未确认。V4 波形任务需要等播放结束才返回完成响应，因此循环波形标签表示请求已发送，不等同设备已完成整段播放。短指令会关联对应的 RPC 回执；失败、超时或被拦截的动作不显示为成功，也不根据模型台词补发或自动重发增量。模型下一回合会收到客观操作回执，避免沿用未执行的叙述。

「模型自判断」开启后，模型仍需结合角色性格和上下文判断是否维持、增加、减少或停止，不保证每句情景台词都引起设备变化。明确说「急停」「停止设备」「stop」「estop」（允许末尾标点）继续优先停机；也可随时点击急停。关闭该模式立即恢复保守解释，并使旧模式下尚未执行的动作失效。

自动运行间隔是两次自动回合开始的最小间隔；不会叠加模型请求，5 秒不代表模型必定在 5 秒内回复。聊天页显示生成状态、耗时和错误；输入消息会优先于尚在生成的自动回合。默认官方 DeepSeek Flash 请求使用非思考模式，空最终回复最多重试一次，两次请求共用一个总超时，不会重试设备动作。

官方 DeepSeek 在 JSON 模式下会将历史助手台词包装为 JSON 后发送，避免历史纯文本与本轮格式要求冲突；包装只作用于请求副本，保留原台词、用户消息和本地历史，不恢复或重放旧动作。首次返回空最终内容时，第二次请求会取消接口的 JSON 输出模式参数；应用依然只接受完整 JSON 与非空台词，不将纯文本或推理内容转换成动作。中断、资源不足或截断的模型输出不能执行设备操作。诊断日志记录结束原因、token 数和是否为空，不记录聊天正文或 API Key。兼容重试不修改已保存的模型名或设置。

聊天请求使用本次服务会话内的唯一编号。连接中断后，点击「确认上次请求」继续查询同一结果，不会重复调用设备。未决请求和草稿暂存在 WebView 会话存储，确认后移除；服务重启或结果过期时不会以旧编号重新执行。不能确认是否已执行的错误要求先检查设备并修改消息，不能直接重复发送原文。

## 持续语音输入

点击「自动运行」旁的麦克风图标，首次允许录音并下载约240 MB的离线识别资源。下载显示进度，可取消后继续；模型使用固定版本和SHA256校验。安装包自带较小的断句模型，大型识别模型保存在应用私有目录，后续无需重复下载。

下载时边写入边校验，不再完整重读刚下载的模型。Android 8.1及以上对已验证文件保存私有校验记录；版本、路径、大小、文件身份或纳秒修改时间发生变化时重新校验。启动页面展示后，后台只预检已有模型，不下载资源、不启动录音。Android 7.0–8.0仍使用完整校验。语音模型本身首次加载仍需要时间，校验缓存不能消除推理引擎加载耗时。

图标点亮后持续收音，说完停顿约0.9秒自动断句，将最终转写文字送入原有聊天流程。模型回复期间仍可说下一句，最多保留5句待发文字；超过队列限制或发送失败时暂停监听并显示待检查的转写。语音不会覆盖输入框内的手写草稿，也不绕过角色判断、A/B上限或急停锁定。明确说出「急停」「停止设备」「stop」或「estop」会优先请求急停并关闭监听；以设备实际状态为准。

麦克风旁的音量条随本机实时音频变化。功能按钮上方只用一行显示临时转写，超过宽度自动滚动到末尾；临时文字可能随识别结果修正，不会提前发送或覆盖键盘草稿。实时预览处理最近最多4秒的声音，与最终识别共用模型，最终整句优先处理；长句的最终发送仍使用完整断句结果。刷新速度取决于设备性能。支持同时收听与播放时，朗读期间音量、预览和最终识别继续工作；关闭监听或回声保护暂停识别时清除预览和音量反馈。

麦克风必须在应用前台由用户开启；开启后切换其他应用或选择「保持后台」仍继续监听和发送，系统通知提供「关闭麦克风」。锁屏、再次点击麦克风、离开聊天页、打开菜单或角色弹窗、切换角色、急停、停止服务或移除应用任务会停止监听，返回或解锁不会自动重新开启。已提交的聊天请求仍按原有回执规则处理，尚未提交的语音不跨页面继续发送。

录音仅在本机内存中用于离线识别，不保存音频文件，也不把录音上传给语音服务。识别后的文字会按现有设置发送给聊天模型。使用sherpa-onnx、SenseVoice和Silero VAD；固定版本、资源来源与各自许可证见`android/app/src/main/assets/voice/`。

## 回复朗读与角色音色

朗读默认关闭，点击麦克风旁的音量图标开启。只读 AI 的新回复正文，不读用户消息、系统提示和设备操作标签，也不补读开启前的历史。新回复到来时立即中止旧朗读，只读最新回复；音色准备期间也只保留最新一条，不积压待播回复。此替换只影响声音，不删除聊天历史或更改已执行的设备动作。语音输入仍最多保留5句待发文字，和朗读的最新回复规则分别处理。

麦克风与朗读开关各自独立。实际启用并持有控制权的硬件回声消除（AEC），或确认正在使用的耳机输出线路，允许全双工：朗读时仍可说话、查看实时转写并发送最终识别结果。确认用户插话后，只停止当前朗读，音量开关继续开启，之后的新 AI 回复仍会朗读。关闭麦克风不会关闭朗读。

没有可用 AEC 且未确认耳机输出线路时，应用保守使用半双工：播放期间暂停识别并提示可连接耳机，结束后恢复监听；线路或回声消除状态变化时重新判断能力。此时不能直接对着扬声器插话，可以关闭朗读后说话或连接耳机。全双工与插话仍需手机实测，实际效果受系统、音量和环境影响，不保证完全消除回声。

录音使用 `VOICE_COMMUNICATION`，在同一录音会话上检查 AEC 的启用状态和控制权，并与播放共享通话音频模式；依据见 [Android 回声消除接口](https://developer.android.com/reference/android/media/audiofx/AcousticEchoCanceler)和[录音预处理说明](https://source.android.com/docs/core/audio/implement-pre-processing)。全双工指声音采集与播放可同时工作，不代表聊天模型会并行执行设备请求。

离线音色采用流式合成与播放，音频生成一部分就开始播放，减少等待整句合成造成的句间停顿。模型初次加载仍需要时间；当真实实时率 RTF 大于 1（生成1秒音频耗时超过1秒）时，播放仍可能出现短暂等待，不能保证所有手机和音色都连续无停顿。

创建角色的两种方式均提供音色选择：女声 A（Kokoro zf_001）、男声 B（Kokoro zm_010）、女声 C（MeloTTS 中文）。新建角色默认女声 C；已有角色保持系统默认，不因升级自动下载音色。字典等小资源随 APK 压缩携带，首次开启所选音色才解压并下载大模型：女声 A 与男声 B 共用约168MB下载，女声 C 约54MB下载。移除当前引擎无需的资源后，两套完整资源分别占约194MB与61MB，不阻塞软件首次进入。音色只影响朗读，不改变角色性格与设备控制规则。

音色资源校验通过后在本机合成，不上传回复正文给语音服务。系统默认要求手机已经安装可用的离线中文音色，否则显示提示。关闭播放、离开聊天、切换角色、急停或锁屏会停止播放，返回不自动重开。模型下载可取消，已准备好的共享资源可以复用。公开音色已由用户试听确认；资源、固定版本和许可证随源码保存。

## 本地构建

要求：JDK 21、Android SDK 35、Build Tools 36、Python 3.12、Node.js 22。Gradle wrapper 和依赖版本在 `android/` 固定。

```text
cd frontend
npm ci
npm run build
cd ..
python packaging/prepare_android.py
cd android
gradlew.bat -PbuildPython=C:/path/to/python.exe assembleRelease
```

Linux/macOS 使用 `bash gradlew -PbuildPython=/path/to/python3.12 assembleRelease`。设置 `ANDROID_HOME` 或在未入库的 `android/local.properties` 内填写 `sdk.dir`。

正式版使用独立签名密钥，签名配置和密钥均保存在源码目录之外。通过环境变量 `COKO_SIGNING_PROPERTIES` 指向 Java properties 文件，必须包含以下四项；示例只是字段格式，不含真实凭据：

```properties
storeFile=C:/private-signing/coko-dg-release.jks
storePassword=YOUR_STORE_PASSWORD
keyAlias=YOUR_KEY_ALIAS
keyPassword=YOUR_KEY_PASSWORD
```

`storeFile` 建议使用绝对路径；Windows 使用正斜杠，避免 Java properties 将反斜杠当作转义。不要把此文件或密钥提交到仓库，也不要在日志里打印密码。保留并备份同一密钥以签署后续更新。

设置环境变量后执行构建（PowerShell）：

```powershell
$env:COKO_SIGNING_PROPERTIES = 'C:/private-signing/coko-dg-signing.properties'
./gradlew.bat -PbuildPython=C:/path/to/python.exe assembleRelease
```

提供有效签名配置时，输出为 `android/app/build/outputs/apk/release/app-release.apk`，`debuggable=false`。未设置该环境变量时，输出 `app-release-unsigned.apk`，需自行签名后才能安装；配置路径无效或缺少必填字段时构建会报错。公开源码不提供发布者的签名凭据。

开发测试可使用 `assembleDebug assembleDebugAndroidTest`，输出位于 `android/app/build/outputs/apk/debug/` 与 `android/app/build/outputs/apk/androidTest/debug/`。debug 使用开发测试签名，不能覆盖正式签名应用。测试应使用专用模拟器，勿为了安装测试版卸载用户的正式版。

GitHub Actions 的 **coko DG Android checks** 工作流只构建并保存测试 APK 为 artifact，不使用正式签名凭据，也不自动创建正式发行版。正式签名 APK 通过 [Releases](https://github.com/romhuj/coko-dg/releases) 分发。

## 架构与数据

Android `RuntimeService` 持有 Chaquopy/Python 服务，`MainActivity` 的 WebView 只允许加载随机本机端口。HTTP/WebSocket 校验内存会话 Cookie、Host 和 Origin；不开放局域网访问，不在 URL 或日志中写会话令牌。语音桥接仅允许当前本机源和主页面：识别通道接受开始、停止和状态查询，回传最终文字、预览、音量与实际全双工状态；朗读通道接受开启、关闭、朗读和停止，校验固定音色标识及会话，支持原子替换旧音频和插话回执。不暴露通用 JavascriptInterface 或设备调用权限。`VoiceCaptureService`以microphone类型前台服务持有录音；它不会随开机、进程重建、解锁或页面返回自行开始录音，锁屏和服务停止时使旧会话失效。

`backend/mobile_runtime.py` 提供 `start(data_root, seed_root, token)`、`estop()`、`stop()`；桌面版默认入口不变。模型密钥、配置和自建角色写在应用私有目录，升级保留，卸载会删除。新默认模型只应用于新安装，不覆盖已有模型或密钥。设置中的空白密钥字段可保留当前已存密钥；更改服务地址时必须重新填写密钥。Android 自动备份/设备迁移默认关闭。种子资源与可写数据目录相互独立。

聊天正文、自动回合和操作回执事务性写入应用私有 SQLite，重新加载或重启后恢复上次聊天；历史按稳定消息ID分页，HTTP 与 WebSocket 同一回执不会重复添加。新建或切换聊天前要求当前请求已完成，成功后停止自动运行并急停；恢复历史不会重执行设备动作或朗读旧消息。存档保留有限长度的模型上下文，存在的原角色会随历史聊天恢复；聊天正文没有固定条数截断。删除角色不删除其旧聊天。存储失败会明确提示，不能借保存失败重复执行设备操作。本机存档不上传云端，卸载应用会删除。

菜单中的历史聊天按置顶优先显示。长按后可置顶、取消置顶或请求删除；删除须再次确认，会删除该聊天正文和上下文。删除其他历史聊天不切换当前聊天，也不更改当前设备状态。删除当前聊天前必须等待回复或未决请求处理完毕，并停止自动运行、急停设备；成功后创建一条空聊天。停止结果无法确认时保留原聊天，不把删除当成停机成功。删除不支持撤销。

`packaging/prepare_android.py` 只复制明确允许的代码、前端构建结果和波形文件，生成空密钥与中性初始角色，不读取个人桌面安装目录。原始 `.pulse` 仍保留在原桌面软件中；APK 使用此前转换的兼容波形。

## 验证边界

后端测试使用假模型、假设备和临时配置；前端设备事件测试使用模拟状态与 API，浏览器测试使用 mock API。`MobileSmokeTest` 仅允许清洁模拟器并使用假设备后端，不发送真实硬件指令。实际 Socket V4 配对、设备反馈和系统后台策略仍需要实机验收。

`VoiceEngineUnitTest`检查静音过滤、文本清理、模型校验、取消和APK内断句资源。`OfflineVoiceModelTest`仅允许专用模拟器，必须预置公开的中文音频与校验后的模型并传入`allow_voice_model_test=true`；执行真实离线识别与断句，不录音、不联网、不请求聊天模型。`MobileSmokeTest`也检查受限语音桥接、重复启动、停止、后台及迟到回调。真实手机的麦克风灵敏度、噪声环境与功耗仍需实测。

`VoiceModelCacheTest`验证文件变更、缓存失效与取消；`VoicePreparationBenchmark`需显式传入`allow_voice_benchmark=true`，分别测量完整校验、缓存和引擎加载，不录音。`VoiceLivePreviewTest`在`allow_voice_model_test=true`时以公开中文 WAV 验证临时识别早于最终断句，并记录识别计算耗时。

`ReplySpeechTest`用假引擎验证替换、插话、关闭与过期回调；`ReplyAudioBufferTest`检查有界音频队列、取消唤醒与文本分片。`VoiceDuplexTest`与假语音服务检查能力判断及全双工播放不会丢失正在说的句子。`OfflineReplyVoiceTest`默认只测资源清单、安全解包与取消；预置经过校验的完整音色后，`allow_offline_tts_test=true`才合成三段固定中文，`allow_offline_tts_playback_test=true`才播放并验证取消/恢复、多句完整播放、首播早于整段合成结束和新回复替换。这些测试不请求聊天模型。原生回调须使用带有`invoke(float[])`方法的显式类，避免 Sherpa JNI 对 Java lambda 泛型擦除的兼容问题。

`test_chat_archive`与`test_mobile_conversations`覆盖跨重启保存、分页、请求去重、存档失败、设置恢复、服务器5秒确认及历史聊天置顶/删除；`test_execution_receipts`验证真实回执状态与波形/强度配对。`PairingProtocolTest`与`PairingBridgeSmoke`使用假剪贴板/启动器验证受限桥接，不实际启动其他应用。`MobileSmokeTest`检查应用名称、新包名及关于链接的外部浏览器 Intent；测试拦截外部跳转，不访问 X 或 GitHub。模拟器无法证明真机回声消除、实际 Socket V4 设备执行或 DG-LAB4 接收剪贴板的效果。

`LiveModelDiagnosticTest` 是单独的实机诊断，默认不运行，必须显式传入 `allow_live_model_probe=true` 并只选择该测试方法。它附加已运行的应用，保留配置与历史，先关闭自动运行并急停，再用当前设置向官方 DeepSeek 请求最多四次模型回执；不执行返回的动作。报告仅保存状态、内容长度和用量等元数据。不要在用户实机上运行整套 instrumentation 或 `MobileSmokeTest`。

后端回归与 GitHub 工作流保持一致：

```text
python -m unittest backend.tests.test_chat_modes backend.tests.test_context_roles backend.tests.test_role_features backend.tests.test_pairing backend.tests.test_mobile_runtime backend.tests.test_chat_requests backend.tests.test_role_management backend.tests.test_turn_reliability backend.tests.test_receipt_edges backend.tests.test_deepseek_empty backend.tests.test_chat_archive backend.tests.test_mobile_conversations backend.tests.test_execution_receipts -q
cd frontend
node --test tests/*.test.cjs tests/*.test.mjs
```

本项目基于 [indhg/AI-for-Coyote](https://github.com/indhg/AI-for-Coyote) 继续开发；原项目完整说明保存在 [UPSTREAM_README.md](UPSTREAM_README.md)，原代码及资源沿用 [CC BY-NC 4.0](LICENSE)。[relay/](relay/) 保留其 [GPL-3.0](relay/LICENSE)。新增语音依赖和模型遵循各自附带的许可证，保留版权与来源说明；TTS 分发含 eSpeak NG GPL 依赖，详见 [TTS NOTICE](android/app/src/main/assets/tts/NOTICE.txt)，不能将整个分发包标为仅含 Apache/MIT。
