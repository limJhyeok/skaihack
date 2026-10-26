"""환경변수 로딩과 경로 설정. 모델 이름은 코드에 두지 않고 환경변수에서만 읽는다."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(APP_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    model_strong: str
    model_fast: str
    workspace_root: Path
    data_dir: Path
    duration_min: int
    rehearsal_count: int
    rehearsal_sec: int
    lang: str


def _is_nested(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def load_settings() -> Settings:
    s = Settings(
        model_strong=os.environ.get("MOCK_MODEL_STRONG", ""),
        model_fast=os.environ.get("MOCK_MODEL_FAST", ""),
        workspace_root=Path(os.environ.get("MOCK_WORKSPACE_ROOT", "~/mock-workspaces")).expanduser().resolve(),
        data_dir=Path(os.environ.get("MOCK_DATA_DIR", APP_ROOT / "data")).resolve(),  # 테스트용 재지정
        duration_min=int(os.environ.get("MOCK_DURATION_MIN", "180")),
        rehearsal_count=int(os.environ.get("MOCK_REHEARSAL_COUNT", "6")),
        rehearsal_sec=int(os.environ.get("MOCK_REHEARSAL_SEC", "300")),
        lang=os.environ.get("MOCK_LANG", "ko"),
    )
    check_paths(s.workspace_root, APP_ROOT)
    return s


def check_paths(workspace_root: Path, app_root: Path) -> None:
    """작업공간 루트와 앱 저장소가 서로 포함 관계면 실행을 거부한다 (봉인 정보 누출 방지)."""
    if _is_nested(workspace_root.resolve(), app_root.resolve()):
        raise SystemExit(
            f"MOCK_WORKSPACE_ROOT({workspace_root})가 앱 저장소({app_root})와 겹칩니다. "
            "서로 다른 상위 디렉터리를 지정하세요."
        )


settings = load_settings()
