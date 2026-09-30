# 設計: S3Storage / S3SessionManager 互換クラス

## 目的

AgentCore などで `S3Storage` や `S3SessionManager` を使うのと同じ書き方で、保存先を TiDB Cloud Filesystem に切り替えられるようにする。

```python
# S3
from strands.session import S3SessionManager
from strands.storage import S3Storage

S3SessionManager("user-42", bucket="my-bucket", prefix="agents/")
S3Storage("my-bucket", prefix="agents/")

# TiDB Cloud Filesystem（この設計）
from strands_tidb_filesystem import TiDBFilesystemSessionManager, TiDBFilesystemStorage

TiDBFilesystemSessionManager("user-42", file_system_id="fs-...", prefix="agents/")
TiDBFilesystemStorage("fs-...", prefix="agents/")
```

保存されるのは、コードが明示的に書いたデータ（会話、エージェントの状態、メモリ、退避したコンテキスト）だけとする。shell が作るファイルは対象外で、S3 版と同じ制約になる（利用者の合意済み）。

## 対応関係

| S3 の概念 | TiDB Cloud Filesystem の対応物 |
|---|---|
| バケット（`bucket`） | ファイルシステム（`file_system_id`） |
| キーのプレフィックス（`prefix`） | パスのプレフィックス（`prefix`） |
| オブジェクト | ファイル |
| 認証（boto3 セッション、IAM） | `fs_token`、または ti プロファイルに保存したトークン（`file_system_id` だけ指定） |
| `region_name` | `region_name`（例 `aws-ap-southeast-1`） |
| `boto_session` / `boto_client_config` | `client`（事前に設定した `TiFSClient`） |
| `endpoint_url`（MinIO など） | 該当なし |

## 1. `TiDBFilesystemStorage`（`S3Storage` 互換）

### シグネチャ

```python
class TiDBFilesystemStorage:
    def __init__(
        self,
        file_system_id: str | None = None,   # S3Storage の bucket に相当。None なら TI_FS_FILE_SYSTEM_ID か fs_token から決まる
        *,
        prefix: str = "",                    # S3Storage と同じ意味。先頭に "/" を補う
        region_name: str | None = None,      # 省略時は TI_REGION_CODE、または ti プロファイルの値
        fs_token: str | None = None,         # 省略時は TI_FS_TOKEN。file_system_id と両方無ければプロファイルの既定値
        client: TiFSClient | None = None,    # boto_session と同じく、他の接続指定とは併用不可
        search_strategy: SearchStrategy | None = None,
    ) -> None: ...
```

- `S3Storage` と同様、`region_name` などの接続指定と `client` を同時に渡した場合は `StorageError` にする。
- 実装済みの `TiDBFilesystemStorage(root="/.strands")` からは破壊的変更になる。未公開なので互換層は作らない。

### 挙動（S3Storage にそろえる）

| メソッド | S3Storage | TiDB 版 |
|---|---|---|
| `write(key, data)` | `put_object`。`search_strategy` があれば索引も作る | `copy-file --from-stdin`（親フォルダは自動作成、常に上書き）。索引作成も同じ |
| `read(key)` | 無ければ `None` | `read-file`。`TiFSNotFoundError` なら `None` |
| `delete(key)` | 無くてもエラーにしない | `delete-file`。`TiFSNotFoundError` は無視 |
| `list(query)` | プレフィックス一致。ページングしてソートし、`prefix` を除いたキーを返す | `list-files` でフォルダを並列にたどる（`find` は100件で打ち切られるため使わない）。同じ形のキーを返す |
| `search(query)` | `search_strategy` が無ければキーワード照合 | `search_strategy` があればそれに任せる。無ければ **TiDB の全文・意味検索**（`ti fs grep`、最大20件）を使う（S3 には無い利点） |
| `namespace(prefix)` | `_NamespacedStorage` を返す | 同じ |

- キーの検証（`..` やバックスラッシュの拒否）には SDK の `_normalize_key` / `_normalize_prefix` を使い、S3Storage と同じ結果にする。
- 失敗はすべて `StorageError` で包み、元の例外は `__cause__` に残す。

### 使い方（S3Storage と同じ場所に差し込める）

```python
storage = TiDBFilesystemStorage("fs-...", prefix="agents/")
agent = Agent(storage=storage)  # SnapshotSessionManager、FileMemoryStore、Context Offloader がこの保存先を使う
agent = Agent(session_manager=SnapshotSessionManager("user-42", storage=storage))
```

## 2. `TiDBFilesystemSessionManager`（`S3SessionManager` 互換）

### シグネチャ

```python
class TiDBFilesystemSessionManager(RepositorySessionManager, SessionRepository):
    def __init__(
        self,
        session_id: str,
        file_system_id: str | None = None,   # S3SessionManager の bucket に相当
        prefix: str = "",
        region_name: str | None = None,
        fs_token: str | None = None,
        client: TiFSClient | None = None,
        **kwargs: Any,
    ): ...
```

### 保存レイアウト（S3SessionManager と同一）

```
/<prefix>/session_<session_id>/
├── session.json
├── agents/agent_<agent_id>/
│   ├── agent.json
│   └── messages/message_<n>.json
└── multi_agents/multi_agent_<id>/multi_agent.json
```

- JSON の形式（`indent=2`、`ensure_ascii=False`）も同じにする。S3 からファイルをそのままコピーすれば移行できる。

### SessionRepository の各メソッド

| メソッド | 実装 |
|---|---|
| `create_session` | `describe-file` で `session.json` の有無を確かめ、既にあれば `SessionException`。無ければ書き込む |
| `read_session` / `read_agent` / `read_message` / `read_multi_agent` | `read-file` して JSON を読む。無ければ `None`。JSON が壊れていれば `SessionException` |
| `delete_session` | `delete-file --recursive`。無ければ `SessionException("... does not exist")`（S3 版と同じ） |
| `create_agent` / `create_message` / `create_multi_agent` | 書き込むだけ |
| `update_agent` / `update_message` / `update_multi_agent` | 既存の値を読んで `created_at` を引き継ぎ、書き込む。無ければ `SessionException` |
| `list_messages(limit, offset)` | `messages/` を `list-files` で一覧し、番号でソートして `offset`/`limit` を適用してから、スレッドプールで並列に読む（S3 版と同じ手順） |

- `session_id` と `agent_id` の検証には、S3 版と同じく SDK の `_identifier.validate` を使う。
- `ti` のエラーは `SessionException` に変換する。例外の種類（`TiFSAuthError` など）は `__cause__` で辿れるようにする。

### 同期呼び出しへの対応

`SessionRepository` のメソッドは同期関数として呼ばれる（`S3SessionManager` も boto3 を同期で呼んでいる）。今の `TiFSClient` は async 専用なので、次のように分ける。

- `TiFSClient` の各コマンドの引数組み立てを共通化する。
- 実行部分は、既存の `run()`（asyncio）に加えて `run_sync()`（`subprocess.run`）を用意する。
- セッションマネージャで使う `read`、`write`、`delete`、`stat`、`list_dir` には、同期版の `*_sync()` を用意する。
- エラーの分類（`_map_error`）とタイムアウトは両者で共有する。

## 3. 性能と、その対策

### 実測値

東京から ap-southeast-1 へ、`ti` 1回あたり:

- 読み込み：約0.5秒
- 書き込み：約0.7秒
- 一覧：約0.4秒

この値はネットワーク経路や実行環境に依存する。

`RepositorySessionManager` は、メッセージが1件増えるたびに次の2つを呼ぶ。

- `create_message`：書き込み1回
- `sync_agent` から呼ばれる `update_agent`：読み込み1回と書き込み1回

素直に実装すると、メッセージ1件あたり約2秒かかる。ツールを1回使うターンではメッセージが4件ほど増えるため、1ターンで約8秒の待ちになる。

### 対策

1. **`created_at` をメモリに覚えておく。** `update_agent` と `update_message` の事前読み込みは、`created_at` を引き継ぐためだけに行われている。自分が作成・読み込みしたものの `created_at` を覚えておけば、読み込みを省ける。これでメッセージ1件あたり約1.4秒になる。挙動は S3 版と変わらない。
2. **推奨構成として `SnapshotSessionManager` + `TiDBFilesystemStorage` を案内する。** SDK のドキュメントも、新規の用途にはこちらを勧めている。1回の呼び出しにつき書き込み1回で済むため、今回の用途ではこちらが実用的である。`TiDBFilesystemSessionManager` は「S3SessionManager から差し替えたい」「マルチエージェントの状態も保存したい」場合向けとする。
3. **（見送り）書き込みをバックグラウンドで行う `background_writes`。** 対策2を推奨構成とすることで合意したため、実装しない。

**実測（実装後）：** `TiDBFilesystemSessionManager` は2メッセージの会話の保存に約4.4秒、`SnapshotSessionManager` と `TiDBFilesystemStorage` の組み合わせはスナップショット1回の保存に約0.8秒だった。

## 4. サンドボックスとの関係

- **決定：** `TiDBFilesystemSandbox`（mount / sync モード）と、それに付随する Plugin は削除した。`search_files` と `find_files` は単体のツールとして残した。
- 削除前の実装は、リポジトリ外の `tifs-sandbox-archive-20260925.tar.gz` に保存してある。
- `Storage` と `SessionManager` は `ti` だけに依存し、マウントも作業フォルダも使わない。AgentCore を含め、`ti` とトークンさえあればどこでも動く。

## 5. テスト

- **ユニットテスト**（偽の `ti` を使う）
  - S3 版と同じレイアウトでファイルができること。
  - JSON の形式。
  - 各メソッドの正常系・異常系：既に存在する、存在しない、JSON が壊れている、権限エラーが `SessionException` になる。
  - `list_messages` のページング。
  - `created_at` の引き継ぎ（キャッシュが効いた場合と、再起動後に読み込む場合の両方）。
- **SDK との結合テスト**（偽の `ti`、モデルもスタブ）
  - `Agent(session_manager=TiDBFilesystemSessionManager(...))` で会話し、別インスタンスで同じ `session_id` を指定して会話が復元されること。
  - `Agent(storage=TiDBFilesystemStorage(...))` で、`SnapshotSessionManager`、`FileMemoryStore`、Context Offloader が正しい名前空間に書き込むこと。
- **互換性テスト**：同じ操作を `S3SessionManager`（moto でモック）と TiDB 版の両方に行い、保存されたファイルの一覧と中身が一致すること。
- **実環境テスト**（`-m integ`）：上の結合テストを本物の `ti` で動かし、1回の呼び出しにかかる時間も記録する。

## 6. 作業の順序

1. `TiFSClient` に同期版の実行経路を加え、引数組み立てを共通化する。
2. `TiDBFilesystemStorage` を新しいシグネチャに変え、`search_strategy` と `namespace` を加える。
3. `TiDBFilesystemSessionManager` を実装し、`created_at` のキャッシュを入れる。
4. テストを書く（ユニット、SDK との結合、S3 との互換、実環境）。
5. README と AgentCore のサンプルを、S3 互換クラスを主役にした構成に書き換える。
6. （任意）`background_writes` を実装する。
