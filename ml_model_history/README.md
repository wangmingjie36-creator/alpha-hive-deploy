# ml_model_history —— ML 模型版本快照

每次 `save_model()` 之后由 `ml_model_guard.snapshot_model_file()` 写入：

- `{stem}-YYYY-MM-DD.json` —— 当天最后一次保存的模型全文（可直接 `load_model` 重放）
- `manifest.jsonl` —— 每次保存追加一行：时间戳 / sha256 / 字节数 /
  `model_type` / `n_samples_seen` / `training_accuracy` / `oos_accuracy`。
  同一天多次保存时快照只留最后一次，**但 manifest 记下了每一次的 sha256**，
  日内是否换过模型看这里。

## 为什么存在

2026-09-04 当日全部 12 份报告的 `ml_prediction.prediction.probability`
**逐位相同** = `0.5899693787928219`（模型退化成常数函数），
2026-08-28 同样（12/14 份恒为 `0.14901620144018954`）。

两次都**无法事后归因**：`ml_model.json` / `ml_model_cache.json` 是原地覆盖，
没有任何版本留存，日志里也没有当天的 ML 训练行。有了这个目录，
下次退化可以直接把当天的模型捞出来、拿当天的输入重放。

## 保留策略

每个 stem 保留最新 `ml_model_guard.RETAIN_SNAPSHOTS` 份（当前 180 天），
`manifest.jsonl` 不裁剪（每行几百字节）。

## ⚠️ 改这个目录前必读

`.gitignore` 有 `ml_model*.json`，会连这里的快照一起吞掉——靠
`!ml_model_history/*.json` 反向规则救回来。**改文件命名规则前先跑
`git check-ignore -v` 确认新名字还在库里**，否则就是「每天写、每天不进库」。

另：本目录同时在 `report_deployer.REPORT_ARTIFACT_PATHS` 与
`_ARTIFACT_PREFIXES` 两张表里（`tests/test_report_deployer_whitelist.py`
盯着两者一致）。缺任何一处 → 每天被改、每天被自动提交跳过、永远挂工作区。
