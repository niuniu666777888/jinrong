"""运行期配置。

原则：所有开关走环境变量，默认值保证「不配任何东西也能跑起来」。
密钥类默认值只用于本地演示，进程启动时会打印提醒。
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # src/app
SRC_DIR = BASE_DIR.parent                            # src

# ---- 服务 ----
HOST = os.environ.get("MVP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MVP_PORT", "8080"))
DB_PATH = os.environ.get("MVP_DB", str(SRC_DIR / "mvp.db"))

# ---- 检索 ----
TOP_K = int(os.environ.get("MVP_TOP_K", "3"))
MIN_SCORE = float(os.environ.get("MVP_MIN_SCORE", "0.35"))

# ---- 对账规则 ----
LOOKBACK_DAYS = int(os.environ.get("MVP_LOOKBACK_DAYS", "7"))     # 差异回溯窗口
HIGH_AMOUNT = float(os.environ.get("MVP_HIGH_AMOUNT", "3000"))    # 高风险金额线
TOKEN_TTL_SECONDS = int(os.environ.get("MVP_TOKEN_TTL", "600"))   # 核验凭证有效期

# ---- 模型 ----
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "mock").lower()     # mock / openai / deepseek
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))

# ---- 令牌签名 ----
# 单进程 MVP：不设则每次启动随机生成，过期凭证在重启后自然失效。
# 多进程/多实例部署必须显式设置 MVP_SECRET，否则各进程签名不一致。
SECRET = os.environ.get("MVP_SECRET") or secrets.token_hex(32)

IS_DEMO_SECRET = not os.environ.get("MVP_SECRET")

# 仅 HTTPS 时下发 Cookie（本地 http 调试保持 False）
COOKIE_SECURE = os.environ.get("MVP_COOKIE_SECURE", "0") == "1"
