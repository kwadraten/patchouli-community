# Patchouli Community

Patchouli 社区扩展与独立插件的受审索引。非内置 Extension 的源码直接提交到本仓库 `extensions/{id}/` 审查；插件作者维护自己的源码和 release，`plugins/{id}.toml` 只保存索引、固定源码修订、发行产物大小与 SHA-256，不复制插件源码。

## 扩展包

每个扩展对应 `extensions/{id}/`，目录内容即随包源码：`main.fsx`（必需）与可选的辅助 `.fsx`、`lib/`、资源文件。`main.fsx` 在顶层 `let extension` 前的 `///` XML 文档声明包身份：

```fsharp
/// <summary>一句话说明。</summary>
/// <patchouli-extension id="{id}" kind="metadata" version="1.0.0" host-api="patchouli.extensions/4">
///   <name>显示名</name>
/// </patchouli-extension>
let extension = ...
```

- `id` 必须与目录名一致（`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`），且不得使用 `metadata.`、`translation.`、`ocr.`、`pdf-text.`、`layout.` 等内置前缀。
- `kind` ∈ `translation|metadata|ocr|retrieval|pdf-text-layer|layout-recognition`；`version` 为严格 SemVer；`host-api` ∈ `patchouli.extensions/3|/4`；恰好一个 `<name>`，允许 `<summary>`/`<remarks>`，文档根节点需恰好一个 `<summary>`。
- 包内只允许常规文件：`#load` / `#r` 的相对路径引用必须在包内解析得到；`extension.yml`、`patchouli-plugin.toml`、`config.json`、`extension-state.json`、`.generations/`、`.lkg/`、`.compiled/` 为宿主保留名，不得提交。
- CI 把每个包打成确定性 zip 发布到 `extensions/{id}/{version}/package.zip`，并在索引中钉住其大小、SHA-256 与最后修改该包的 commit。

## 插件索引

每个插件对应 `plugins/{id}.toml`：

```toml
id = "author.plugin-id"
repository = "https://github.com/author/repository"
source_revision = "<完整 Git commit>"

[[artifacts]]
platform = "win-x64"
url = "https://github.com/author/repository/releases/download/v1.0.0/plugin-win-x64.zip"
size = 123456
sha256 = "<小写 SHA-256>"
```

固定修订中的 `patchouli-plugin.toml` 必须包含相同的 ID 和仓库，以及 `name`、`description`、`version`、`license`、`host-api = "patchouli.plugins/1"`、静态 VFS/动作声明。当前宿主只接受 `library.grants = []`。CI 会读取固定修订的清单，并实际下载每个发行产物复核大小与 SHA-256。

## 签名与发布

GitHub Pages 发布 `index/v1/index.json` 和 `index.json.minisig`。签名使用标准 minisign 格式：BLAKE2b-512 预哈希、Ed25519 内容签名，以及可信注释的独立 Ed25519 签名。客户端必须同时验签并复核下载包 SHA-256；缓存也必须通过同一验签。

当前公钥位于 `keys/`。签名种子和 key ID 仅保存在本仓库 GitHub Actions secrets 中。轮换密钥时新增公钥并让宿主在一段时间内同时信任新旧公钥，随后再移除旧公钥。

索引签名、哈希和审查不构成插件进程的安全沙箱。插件以当前用户权限运行；安装界面必须明确展示其声明和这一限制。
