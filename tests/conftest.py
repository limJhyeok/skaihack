import os
import tempfile

from cryptography.fernet import Fernet

# app.config가 import 시점에 경로를 검사하므로, 테스트용 작업공간 루트와 봉인 키를 먼저 지정한다.
os.environ["MOCK_WORKSPACE_ROOT"] = tempfile.mkdtemp(prefix="mockws_test_")
os.environ["MOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="mockdata_test_")
os.environ.setdefault("MOCK_SEAL_KEY", Fernet.generate_key().decode())
