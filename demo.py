# save as: split_code.py

import os

extensions = ('.py', '.html')
skip_dirs = {'venv', '__pycache__', 'migrations', '.git', 'node_modules'}

# Define batches
batches = {
    'batch1_core.txt': ['models.py', 'extensions.py', 'config.py', 'run.py', '__init__.py'],
    'batch2_routes.txt': ['admin.py', 'auth.py', 'groups.py', 'loans.py', 'wallets.py'],
    'batch3_services.txt': ['admin_service.py','interest_service.py', 'authorization_service.py', 'loan_service.py',
                            'membership_service.py', 'wallet_service.py' ],
    'batch4_templates.txt': ['.html'],
}

for batch_name, keywords in batches.items():
    with open(batch_name, 'w', encoding='utf-8') as out:
        for root, dirs, files in os.walk('.'):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file in files:
                if any(kw in file for kw in keywords):
                    filepath = os.path.join(root, file)
                    out.write(f"\n{'='*50}\n")
                    out.write(f"FILE: {filepath}\n")
                    out.write(f"{'='*50}\n\n")
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            out.write(f.read())
                    except:
                        pass
    print(f"✅ Created: {batch_name}")