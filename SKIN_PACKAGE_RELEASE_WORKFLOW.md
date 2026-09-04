# Chosen 皮肤包发布流程

## 目标

每个皮肤资源包从**本版全量 ZIP**开始发布。工具上传全量到 R2 / GitHub / 香港；若提供**上一版全量 ZIP**，还会尝试生成差量包（变更文件 + `delete_list.json`），在差量 < 全量 70% 时一并发布。

总纲见 `E:\ChosenSkin2.0\Chosen\发版流程.md`。

固定项目：

- 本地工具目录：`E:\ChosenSkin2.0\woguaskin`
- 发布工具：`E:\ChosenSkin2.0\woguaskin\publish_skin_release.py`
- 启动器：`E:\ChosenSkin2.0\woguaskin\启动皮肤包发布工具.bat`
- GitHub 仓库：`https://github.com/Chosen11111111/woguaskin`
- Git 分支：`main`
- manifest：`E:\ChosenSkin2.0\woguaskin\skin-manifest.json`
- raw manifest：`https://raw.githubusercontent.com/Chosen11111111/woguaskin/main/skin-manifest.json`
- R2 Bucket：`lolskin`
- R2 对象目录：`woguaskins/`（仅全量）
- 香港全量：`/woguaskins/`；香港差量：`/woguaskins-delta/`（不上 R2）
- R2 公网域名：`https://cdn.chosen.cc.cd/woguaskins`

## ZIP 要求

ZIP 根目录必须直接包含：

~~~text
resources/
skins/
version.json
~~~

`version.json` 中的版本号必须是有效版本号，例如：

~~~json
{
  "version": "0.0.5"
}
~~~

禁止出现：

- 外层版本目录，例如 `skins0.05/resources/`
- `Chosen.exe`、`ChosenUpdater.exe`
- `_internal/`、`Chosendata/`、`.git/`
- `skin-manifest.json`
- 其他 `.zip` 文件
- 路径穿越、重复路径或损坏文件

工具还会调用 Chosen 生产校验器进行第二次校验。

## 本机 R2 配置

发布工具从 Windows 用户环境变量读取 R2 配置：

~~~text
R2_ENDPOINT
R2_BUCKET
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
~~~

当前值应为：

~~~text
R2_ENDPOINT=https://<Account ID>.r2.cloudflarestorage.com
R2_BUCKET=lolskin
~~~

`R2_ACCESS_KEY_ID` 和 `R2_SECRET_ACCESS_KEY` 使用 Cloudflare R2 API Token 创建页面中的同名字段。不要使用 Token 名称，也不要把 `cfut_` 字段当成 S3 Access Key。

建议在 Windows 的“系统属性 → 高级 → 环境变量 → 用户变量”中保存一次。这样双击启动器也能读取。不要把密钥写入 Python、批处理、manifest 或 GitHub。

Token 权限只需要：

~~~text
Object Read & Write
Bucket：lolskin
~~~

项目 Python 环境需要安装 boto3；缺少时安装：

~~~powershell
& "E:\ChosenSkin2.0\Chosen\.venv\Scripts\python.exe" -m pip install boto3
~~~

## 发布顺序

~~~text
选择本版全量 ZIP（必选）
-> 可选：选择上一版全量 ZIP
-> 校验 ZIP / 若有上一版则算差量（≥70% 全量则跳过差量）
-> 全量：R2 + GitHub Release + 香港 /woguaskins/
-> 差量（若有）：GitHub 第二资产 + 香港 /woguaskins-delta/（不上 R2）
-> 探测下载地址
-> 写入并推送 skin-manifest.json（含或不含 delta_*）
-> 验证远程 manifest
~~~

全量 R2 Key：`woguaskins/<实际 ZIP 文件名>`（例 `woguaskins/skins0.05.zip`）。  
差量示例名：`skins0.05-from-0.04.zip`。

命令行：

~~~powershell
cd E:\ChosenSkin2.0\woguaskin
..\Chosen\.venv\Scripts\python.exe publish_skin_release.py skins0.05.zip
..\Chosen\.venv\Scripts\python.exe publish_skin_release.py skins0.05.zip --previous-zip skins0.04.zip
~~~

## 启动工具

配置好 Windows 用户环境变量后，双击：

~~~text
E:\ChosenSkin2.0\woguaskin\启动皮肤包发布工具.bat
~~~

界面中：

1. 选择**本版全量 ZIP**，确认版本与 SHA-256。
2. （可选）选择**上一版全量 ZIP**；不选则只发全量。
3. 点击「开始发布」，查看日志。

发布在后台线程运行，窗口不会因上传卡死。香港需一次性配置 OpenResty `/woguaskins-delta/`。

## manifest 格式

全量字段始终存在。若成功发布差量，另含 `delta_*`（示例）：

~~~json
{
  "version": "0.0.5",
  "download_url": "https://download.52015002.xyz/woguaskins/skins0.05.zip",
  "download_url_backup": [
    "https://cdn.chosen.cc.cd/woguaskins/skins0.05.zip",
    "https://v4.gh-proxy.org/https://github.com/Chosen11111111/woguaskin/releases/download/skin-v0.0.5/skins0.05.zip"
  ],
  "size": 123456789,
  "sha256": "全量 64 位小写 SHA-256",
  "delta_from": "0.0.4",
  "delta_download_url": "https://download.52015002.xyz/woguaskins-delta/skins0.05-from-0.04.zip",
  "delta_download_url_backup": [
    "https://gh-proxy.com/https://github.com/Chosen11111111/woguaskin/releases/download/skin-v0.0.5/skins0.05-from-0.04.zip",
    "https://cdn.gh-proxy.org/https://github.com/Chosen11111111/woguaskin/releases/download/skin-v0.0.5/skins0.05-from-0.04.zip",
    "https://v4.gh-proxy.org/https://github.com/Chosen11111111/woguaskin/releases/download/skin-v0.0.5/skins0.05-from-0.04.zip"
  ],
  "delta_size": 1234567,
  "delta_sha256": "差量 64 位小写 SHA-256",
  "release_tag": "skin-v0.0.5"
}
~~~

客户端无论使用哪个地址下载，都必须校验：

~~~text
文件大小 == manifest.size
SHA-256 == manifest.sha256
ZIP 结构和版本 == 预期值
~~~

## 停止条件

遇到下面任一情况，工具必须停止，不推送 manifest：

- ZIP 结构错误、version.json 缺失或版本号无效
- 生产 ZIP 校验失败
- R2 凭据缺失或上传失败
- R2 同名对象存在但大小不一致
- R2 主地址不是 206、总大小不对或不是 ZIP
- GitHub Release/tag 已存在
- GitHub 资产上传失败
- GitHub 代理测试失败
- Git 暂存区已有其他文件
- push 或远程 manifest 验证失败

R2 已上传但后续 GitHub 阶段失败时，不要手动修改 manifest；先处理错误，再决定是否清理 R2 对象。

## 发布前检查

- [ ] 选择的是最终 ZIP，不是资源目录或增量文件。
- [ ] ZIP 根目录直接包含 resources、skins、version.json。
- [ ] version.json 的版本号是新版本。
- [ ] Windows 用户环境变量已配置，且没有把密钥写入项目。
- [ ] R2 Bucket 是 Standard，权限限制到 lolskin。
- [ ] R2 和 GitHub 使用同一个 ZIP。
- [ ] 工具日志显示 R2 测试和 GitHub 代理测试都通过。
- [ ] 只提交 skin-manifest.json。

不要执行 git add .、git commit -a 或 git push --force。
