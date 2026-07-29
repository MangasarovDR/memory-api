#!/usr/bin/env python3
"""Добавляет Memory API хук и env-переменные в ~/.claude/settings.json."""
import json, os

settings_path = os.path.expanduser('~/.claude/settings.json')
env_path = '/opt/memory-api/.env'

with open(env_path) as f:
    for line in f:
        if line.startswith('MEMORY_API_KEY='):
            api_key = line.strip().split('=', 1)[1]
            break

with open(settings_path) as f:
    settings = json.load(f)

settings.setdefault('env', {})
settings['env']['MEMORY_API_URL'] = 'https://memory.automatiko.ru'
settings['env']['MEMORY_API_KEY'] = api_key

settings.setdefault('hooks', {})
settings['hooks'].setdefault('PostToolUse', [])

hook_cmd = 'python3 /opt/memory-api/cc_memory_sync.py'
exists = any(
    any(h.get('command') == hook_cmd for h in e.get('hooks', []))
    for e in settings['hooks']['PostToolUse']
)
if not exists:
    settings['hooks']['PostToolUse'].append({
        "matcher": "Write|Edit",
        "hooks": [{"type": "command", "command": hook_cmd, "timeout": 10, "async": True}]
    })

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)

print(f'OK: {settings_path} обновлён')
print(f'  env.MEMORY_API_URL = https://memory.automatiko.ru')
print(f'  env.MEMORY_API_KEY = ***{api_key[-4:]}')
print(f'  hooks.PostToolUse [Write|Edit] → {hook_cmd}')
