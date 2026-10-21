# fd-shilun 服务端

实论门面服务端：论文检索、公文模板、写作辅助（引用核验 / 公文国标排版 / 规则校对）。

## 环境变量

| 变量 | 缺省 | 说明 |
| --- | --- | --- |
| `FD_SHILUN_DB` | `~/.fd-shilun/shilun.db` | 写作项目/版本/引用的 SQLite 路径 |
| `FD_SHILUN_LLM_API_KEY` | 未配置 | LLM 网关密钥；未配置时提纲走确定性引擎 |
| `FD_SHILUN_CITE_VERIFY_MODE` | `shadow` | 引用核验模式：`off` / `shadow` / `enforce` |
| `FD_SHILUN_CN_CHANNELS` | 未配置 | 中文文献授权通道（JSON 数组，见 `fd_shilun_api/channels.py`） |
| `FD_SHILUN_CN_OPEN_FALLBACK` | 开 | 中文检索的开放元数据兜底；设 `0/false/off` 关闭（回滚档） |

### 引用核验模式（`FD_SHILUN_CITE_VERIFY_MODE`）

三级核验（可解 → 真实 → 支持）的处置开关。缺省与非法值一律回落 `shadow`。

| 模式 | 报告 | 正文处置 |
| --- | --- | --- |
| `off` | 产出 | 只剥「不可解」标记（历史行为） |
| `shadow` | 产出 | 同 `off`，**逐字节一致** |
| `enforce` | 产出 | 额外剥除二级/三级 `block` 的引用；`pending` 永不改动 |

**上线节奏**（design「Migration Plan」）：

1. 以 `shadow` 上线，只观察报告；
2. 用历史稿件回放，看 block / pending 比例与误报：`python3 scripts/replay_shadow.py -o /tmp/replay.json`；
3. 比例达标后切 `enforce`；
4. 回滚：把变量切回 `shadow` 并重启。回滚后成稿行为与上线前完全一致。

切换前建议先跑演练，确认开关与回滚都符合预期：

```bash
python3 scripts/rollback_drill.py    # PASS 才切 enforce
```

## 中文文献检索：授权通道优先 + 开放元数据兜底

中文文献检索按「已授权通道优先、开放元数据兜底」执行（spec `shilun-serious-writing`
「论文检索」）：

1. **配了可用授权通道** → 经通道检索，结果标注 `provenance.authorization`
   （来源通道 + 授权依据）。
2. **没配通道，或通道无命中** → 走开放元数据（Crossref `query.bibliographic`
   → OpenAlex `search`），每条结果标 `coverage=partial`、`metadata_only=true`，
   响应附 `coverage_note` 说明覆盖不完整。免费、无需密钥，但只含已注册 DOI
   或已被收录的条目，未收录的中文期刊与学位论文查不到。
3. **中文题名零命中**且配置了 LLM 网关 → 自动用英文检索词再查一次（该主题的
   文献可能全著录为英文题名），命中即返回并在 `coverage_note` 里说明「已自动用
   英文检索词补充检索」。**未配置网关时不发生**，行为与之前完全一致。
4. **两者都无命中** → 空结果 + `reason=not_indexed`，`detail` 注明覆盖不完整。

`reason=no_authorized_channel` **不再由关键词检索产生**，它只保留给需要原文
全文的能力（引用核验三级「支持」拿不到全文时标待核）。
任何路径都**不以浏览器自动化抓取商业文献库替代**（`tests/test_no_browser_scraping.py` 守卫）。

回滚：`FD_SHILUN_CN_OPEN_FALLBACK=0` 时中文检索回到「仅授权通道」，
无通道即返回 `no_authorized_channel`，且一次外部请求都不发。

### 授权通道配置与 BYOK 凭据

```json
[
  {"id": "wanfang", "kind": "http", "license": "合同号或授权依据",
   "base_url": "https://api.example.com/v1/search",
   "api_key_env": "WANFANG_API_KEY"}
]
```

凭据有两条来源，**显式提供优先于环境变量**：

- 环境变量：`export WANFANG_API_KEY=...`（运维/单租户部署）；
- BYOK（使用者自备）：请求体带 `channel_credentials`，键为通道 id：

```json
{"query": "乡村振兴", "channel_credentials": {"wanfang": "<客户的 key>"}}
```

BYOK 凭据只在单次调用的内存中存活：不写入 `ChannelConfig`、不落库、不进日志、
不在响应中回显（`tests/test_open_metadata.py` 的凭据守卫测试锁定这条边界）。
环境变量没设**不再等于通道写坏**——该通道进入「凭据待提供」状态，补凭据即可用。

## 本地开发

```bash
pip install -e ".[dev]"
pytest
uvicorn fd_shilun_api.main:app --port 8010
```

## 验收脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/replay_shadow.py` | 历史稿件 shadow 回放，产出三态比例与误报统计（task 5.2） |
| `scripts/rollback_drill.py` | enforce 切换与回滚演练，PASS 才可切闸（task 5.3） |
| `scripts/e2e_walkthrough.py` | 端到端走查 |
