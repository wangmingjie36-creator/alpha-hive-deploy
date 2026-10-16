"""把 sandbox_base（生产数据根的 APFS 克隆 + db_backups/pheromone_2026-10-12.db）还原成 10-12 扫描开始前（14:11:55 PDT）的状态。
只写 SANDBOX。规则：
  1. 路径含 2026-10-12 的产物删掉（扫描当天产出）；
  2. 备份仓跟踪、且生产里 mtime ≥ 14:00 的文件 → 取备份仓 435482c（10-09 扫描后 = 10-12 扫描前；10-12 白天开发会话改过的 09-17 文件 mtime < 14:00，不在此列）；
  3. 未跟踪的缓存（cache/ data_cache/ reddit_cache/）mtime ≥ 扫描开始 14:11:55 → 删（扫描自己写的，多为失败兜底值）；
     14:00–14:11 是 Step 1（data_fetcher）写的，生产扫描当时就读得到 ⇒ control 保留；
  4. sentiment_baseline.db 由 435482c 的 SQL 导出重建；
记录每一步到 prep_manifest.json。"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
PROD, SANDBOX, MODE = sys.argv[1], sys.argv[2], sys.argv[3]   # MODE: control | corrected
# 第 4 参（可选）：文件清单与 mtime 的参照目录。v0.45.484 写回生产之后，生产的 iv_history 等 mtime 变成了写回时刻，
# 再拿生产当参照会把沙箱里扫描前的 IV 历史当「扫描写的缓存」删掉 ⇒ 写回之后一律以 sandbox_base（写回前的 APFS 克隆，
# mtime 原样保留）为参照。备份仓 git 仍从 PROD 读。
MREF = sys.argv[4] if len(sys.argv) > 4 else PROD
BK = os.path.join(PROD, "_git_backup"); REF = "435482c"
T_SYNC = datetime(2026, 10, 12, 14, 0, 0).timestamp()
T_SCAN = datetime(2026, 10, 12, 14, 11, 55).timestamp()
T_BK = datetime(2026, 10, 9, 14, 45, 53).timestamp()
man = {"deleted_dated": [], "restored_from_backup": [], "deleted_cache": [], "kept_step1_cache": [], "notes": []}
def git(*a, text=True):
    return subprocess.run(["git", "-C", BK, *a], check=True, capture_output=True, text=text).stdout
SKIP_TOP = {"_git_backup", "_manual_backups", "db_backups"}
for root, dirs, files in os.walk(MREF):
    rel_root = os.path.relpath(root, MREF)
    if rel_root.split(os.sep)[0] in SKIP_TOP:
        dirs[:] = []; continue
    for fn in files:
        rel = os.path.normpath(os.path.join(rel_root, fn))
        if rel.startswith("pheromone.db"):
            continue
        sp = os.path.join(SANDBOX, rel)
        if "2026-10-12" in rel:
            if os.path.exists(sp):
                os.remove(sp); man["deleted_dated"].append(rel)
            continue
        mt = os.path.getmtime(os.path.join(MREF, rel))
        if mt < T_SYNC:
            continue
        tracked = subprocess.run(["git", "-C", BK, "cat-file", "-e", f"{REF}:{rel}"], capture_output=True).returncode == 0
        if tracked:
            blob = subprocess.run(["git", "-C", BK, "show", f"{REF}:{rel}"], check=True, capture_output=True).stdout
            with open(sp, "wb") as f:
                f.write(blob)
            os.utime(sp, (T_BK, T_BK))   # 还原文件的 mtime 也回到 10-09 扫描后，免得 TTL / 「今天训练过」判断被现在的写入时刻带偏
            man["restored_from_backup"].append(rel)
        elif rel.split(os.sep)[0] in ("cache", "data_cache", "reddit_cache"):
            if mt >= T_SCAN or MODE == "corrected":
                if os.path.exists(sp):
                    os.remove(sp); man["deleted_cache"].append(rel)
            else:
                man["kept_step1_cache"].append(rel)
        else:
            man["notes"].append(f"untracked post-14:00 file left as-is: {rel}")
# sentiment_baseline.db ← 435482c SQL 导出
sb = os.path.join(SANDBOX, "sentiment_baseline.db")
if os.path.exists(sb):
    os.remove(sb)
c = sqlite3.connect(sb)
for part in ("db_exports/sentiment_baseline/sentiment_baseline.sql", "db_exports/sentiment_baseline/_schema_extra.sql"):
    c.executescript(git("show", f"{REF}:{part}"))
c.commit()
man["notes"].append(f"sentiment_baseline rows={c.execute('select count(*) from sentiment_baseline').fetchone()[0]} max_date={c.execute('select max(date) from sentiment_baseline').fetchone()[0]}")
c.close()
# chroma：删掉 10-12 扫描写入的记忆（metadata date=2026-10-12）
try:
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    import chromadb
    cl = chromadb.PersistentClient(path=os.path.join(SANDBOX, "chroma_db"))
    for col in cl.list_collections():
        col = cl.get_collection(col.name if hasattr(col, "name") else col)
        got = col.get(where={"date": "2026-10-12"}, include=[])
        if got["ids"]:
            col.delete(ids=got["ids"])
        man["notes"].append(f"chroma {col.name}: deleted {len(got['ids'])} entries dated 2026-10-12; remaining {col.count()}")
except Exception as e:
    man["notes"].append(f"chroma cleanup FAILED: {type(e).__name__}: {e}")
json.dump(man, open(os.path.join(SANDBOX, "_prep_manifest.json"), "w"), ensure_ascii=False, indent=1)
print({k: len(v) for k, v in man.items()}); print("\n".join(man["notes"]))
