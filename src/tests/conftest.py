"""测试装配：导入 app 之前先把库指到临时目录。"""

import os
import pathlib
import sys
import tempfile

SRC = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

_TMP = tempfile.mkdtemp(prefix="jinrong-mvp-test-")
os.environ["MVP_DB"] = os.path.join(_TMP, "test.db")
os.environ["MVP_SECRET"] = "test-only-secret-not-for-production"
os.environ["LLM_PROVIDER"] = "mock"
