# Patchouli Community

Patchouli 社区扩展与独立插件的受审索引。插件作者维护自己的源码和 release；本仓库只保存索引、固定源码修订、发行产物大小与 SHA-256，不复制插件源码。

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
