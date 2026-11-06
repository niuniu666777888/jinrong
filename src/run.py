"""一条命令启动 MVP。"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from app import config  # noqa: E402

if __name__ == "__main__":
    print(f"→ http://{config.HOST}:{config.PORT}")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info")
