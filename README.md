# coko DG

在 Android 手机上通过角色聊天、语音与自动回合控制 DG-LAB 设备。设备由 **DG-LAB 4 App 的 Socket V4** 连接，coko DG 在本机运行界面和控制服务，无需电脑保持运行。

**0.7.0 是本仓库的首个正式发行版。**

当前 `main` 包含后续优化：通道名称、角色信息编辑与相册头像。源码构建版本仍为 0.7.0（versionCode 2），未新增发行版；已发布的 `v0.7.0` APK 保持原样。

[下载 APK](https://github.com/romhuj/coko-dg/releases/latest) · [0.7.0 更新说明](RELEASE_NOTES_0.7.0.md) · [构建与完整使用说明](ANDROID.md) · [反馈](https://x.com/_Good_Dick_)

## 安装与开始使用

需要 Android 7.0 或以上，支持 arm64-v8a 与 x86_64。聊天模型、联网角色搜索和公共中继需要网络；本应用不提供直接蓝牙连接。

1. 从本仓库 Releases 下载并安装正式签名的 APK。
2. 进入「设置」，填写自己的模型 API Key，测试连接并保存。默认接口为 DeepSeek；「前往获取」在外部浏览器打开 API Key 页面。安装包不含任何用户密钥。
3. 启动后的设备连接弹窗可显示二维码、复制链接，或「复制并跳转」到 DG-LAB 4。在官方 App 的 Socket V4 入口完成连接。
4. 检查 A/B 通道上限，再显式「解除急停」。解除本身不会开始输出；可以聊天，或在输入框上方开启自动运行。

0.7.0 使用新包名 `io.github.romhuj.cokodg` 和独立正式签名，与旧 `org.coyote.mobile` 预览版可以并存。**不会覆盖旧版，也不会自动迁移旧版的聊天、角色、配置、密钥或下载的语音资源**；首次使用需要重新设置。今后正式版升级需使用相同包名与签名，才能保留应用数据。

## 主要功能

- 角色聊天与自动运行共用情景上下文；支持搜索创建和自定义创建角色、昵称与角色音色。
- A/B 独立上限、六档输出强度、236 组波形；设置波形时同步设置同通道强度，回复下方分别显示实际操作回执。
- 长期保存聊天与设置。在菜单历史聊天列表中长按一条聊天，可置顶、取消置顶或删除；菜单右上角可新建聊天。
- 持续离线语音输入、实时音量与转写预览、回复朗读。支持硬件回声消除或耳机线路时可以同时收听和播放；其他情况下使用回声保护。
- 设置「高级」下方的「关于」提供反馈和本公开源仓库入口，均调用外部浏览器。
- 急停按钮在各界面顶部保持可用；息屏请求急停并停止本地服务，亮屏切换应用可继续运行。

自动运行间隔不是模型响应时间保证。设备操作受有效上限、连接状态和急停锁定约束；未确认的动作不会根据模型台词当作执行成功。网络或系统异常时，应以 DG-LAB App 与设备实际状态为准。

语音资源在首次使用相关功能时下载，不阻塞首次进入应用。录音只在本机识别；转写文字和聊天会发给用户配置的模型服务。聊天、配置和资源保存在应用私有目录，卸载应用会删除。

## 从源码构建

依赖 JDK 21、Android SDK 35 / Build Tools 36、Python 3.12 与 Node.js 22。完整构建步骤、独立签名配置、测试方法及实机验证范围见 [ANDROID.md](ANDROID.md)。

```text
cd frontend
npm ci
npm run build
cd ..
python packaging/prepare_android.py
cd android
gradlew.bat -PbuildPython=C:/path/to/python.exe assembleRelease
```

未设置 `COKO_SIGNING_PROPERTIES` 时，`assembleRelease` 生成未签名 APK；正式发布使用发布者保存在仓库之外的签名配置与密钥。GitHub Actions 仅构建用于测试的 debug APK，不能用于覆盖正式发行版。

## 来源与许可

coko DG 基于 [indhg/AI-for-Coyote（Coyote in Cradle）](https://github.com/indhg/AI-for-Coyote) 继续开发，新增 Android 本机服务、移动界面、语音、聊天存档与相关控制流程。上游完整项目说明保存在 [UPSTREAM_README.md](UPSTREAM_README.md)。本项目不是 DG-LAB 官方应用。

- 原项目代码、资源与文档保留 [CC BY-NC 4.0 许可和署名](LICENSE)，该许可包含非商业限制。
- `relay/` 来自 [dungeonlab-open/dglab-websocket-server](https://github.com/dungeonlab-open/dglab-websocket-server)，保留 [GPL-3.0 许可](relay/LICENSE)。
- 离线识别与朗读依赖、模型分别遵循其附带许可，见 [语音识别来源与许可](android/app/src/main/assets/voice/NOTICE.txt) 和 [朗读来源与许可](android/app/src/main/assets/tts/NOTICE.txt)。其中包含 eSpeak NG 的 GPL 条款，整个分发包不能标为仅含 Apache/MIT。

本仓库不包含个人运行配置、API Key、聊天数据库、录音、签名密钥或原始私人内容包。
