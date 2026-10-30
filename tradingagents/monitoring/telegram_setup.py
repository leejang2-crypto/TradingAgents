"""Interactive local setup; credentials never pass through browser URLs or argv."""

import fcntl
from getpass import getpass
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile

from dotenv import dotenv_values, set_key

from .telegram import PREFIX, TelegramError, api_call


ENV_PATH = Path(__file__).resolve().parents[2].parent / ".env"


def save_credentials(path, token, chat):
    """Preserve unrelated shared .env keys and atomically replace all three fields."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("공용 .env 심볼릭 링크는 직접 확인 후 수정하세요.")
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token) or not re.fullmatch(r"-?[1-9][0-9]{0,19}", chat):
        raise ValueError("토큰 또는 대화 ID 형식이 잘못되었습니다.")
    with path.with_name('.telegram-setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.telegram-env-', delete=False) as stream:
                temp = Path(stream.name)
            if path.exists():
                shutil.copyfile(path, temp)
            temp.chmod(0o600)
            for key, value in {'BOT_TOKEN': token, 'CHAT_ID': chat, 'ENABLED': '1'}.items():
                set_key(str(temp), PREFIX + key, value)
            with temp.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp and temp.exists():
                temp.unlink()


def matching_chat(updates, challenge):
    matches = {}
    for update in updates if isinstance(updates, list) else []:
        message = update.get('message') or {}
        chat = message.get('chat') or {}
        if message.get('text', '').strip() == challenge and chat.get('type') == 'private':
            matches[str(chat['id'])] = chat
    if len(matches) != 1:
        raise ValueError("본인 확인 메시지를 찾지 못했습니다. 봇 개인 대화에 표시된 문구를 보내고 다시 실행하세요.")
    return next(iter(matches))


def main():
    print('TradingAgents 텔레그램 알림 설정 (매매 주문 없음)')
    print(f'저장 위치: {ENV_PATH}')
    existing = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    token = getpass('BotFather 봇 토큰 (입력 내용 숨김, Enter=기존 값): ').strip() or existing.get(PREFIX + 'BOT_TOKEN', '')
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token):
        raise ValueError('올바른 BotFather 토큰을 입력하세요.')
    bot = api_call(token, 'getMe', {})
    print(f"연결할 봇: @{bot.get('username', '확인됨')}")
    chat = input('수신 chat_id (Enter=본인 대화 자동 확인): ').strip()
    if not chat:
        challenge = '/start ' + secrets.token_urlsafe(12)
        print(f'텔레그램 @{bot.get("username", "")} 개인 대화에 아래 문구를 보내세요:')
        print(challenge)
        input('전송 후 Enter: ')
        chat = matching_chat(api_call(token, 'getUpdates', {'limit': 100, 'timeout': 0}), challenge)
    if not re.fullmatch(r'-?[1-9][0-9]{0,19}', chat):
        raise ValueError('chat_id는 숫자로 입력하세요. 그룹 ID는 음수일 수 있습니다.')
    target = api_call(token, 'getChat', {'chat_id': chat})
    label = target.get('title') or target.get('first_name') or target.get('username') or '대화'
    print(f'수신 대상: {label} · {chat}')
    if input('이 대상에 테스트 알림을 보내고 저장할까요? [y/N]: ').strip().lower() != 'y':
        print('취소했습니다. 저장하거나 전송하지 않았습니다.')
        return 0
    api_call(token, 'sendMessage', {'chat_id': chat, 'text': '[TradingAgents] 연결 테스트 성공\n알림 전용입니다. 매매 주문은 실행하지 않았습니다.'})
    save_credentials(ENV_PATH, token, chat)
    print('저장 완료. 실행 중인 TradingAgents를 정상 종료하고 다시 실행하세요.')
    print('대시보드: 설정 > 텔레그램 알림에서 활성화·항목 선택·전송 상태 확인')
    print('토큰을 Git 또는 대화창에 올리지 마세요. 터미널의 기존 환경변수가 있다면 .env보다 우선합니다.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n설정을 취소했습니다.')
        raise SystemExit(1)
    except TelegramError as exc:
        print(f'텔레그램 확인 실패: {exc.code}. 토큰·대화 ID·봇 차단 여부·네트워크를 확인하세요.')
        raise SystemExit(1)
    except (OSError, ValueError):
        print('설정 실패: 입력값과 공용 .env 쓰기 권한을 확인하세요. 비밀 값은 출력하지 않습니다.')
        raise SystemExit(1)
