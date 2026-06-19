import json
import os
import hashlib
import secrets
import shutil

DATA_DIR = 'data'
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
BACKUP_FILE = os.path.join(DATA_DIR, 'users_backup_before_hash.json')

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return f'{salt}${pwd_hash}'

def is_already_hashed(password):
    return isinstance(password, str) and '$' in password and len(password.split('$')[0]) == 32

def main():
    with open(USERS_FILE) as f:
        users = json.load(f)

    shutil.copy(USERS_FILE, BACKUP_FILE)
    print(f'Backup saved to {BACKUP_FILE}')

    changed = 0
    for u in users:
        pwd = u.get('password', '')
        if is_already_hashed(pwd):
            print(f'Skipping {u["username"]} (already hashed)')
            continue
        u['password'] = hash_password(pwd)
        changed += 1
        print(f'Hashed password for {u["username"]}')

    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

    print(f'\nDone. {changed} password(s) hashed out of {len(users)} user(s).')

if __name__ == '__main__':
    main()
