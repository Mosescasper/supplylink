gitignore_content = """
# Environment / secrets
.env
.env.*
!.env.example

# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
.eggs/

# Virtual environments
venv/
env/
.venv/
ENV/

# Flask
instance/
.webassets-cache

# Database
*.db
*.sqlite
*.sqlite3

# Migrations working files (keep the migrations folder itself if you use Flask-Migrate)
# Uncomment the next line only if you don't want migration versions tracked (usually you DO want them):
# migrations/

# Uploads (user-generated content shouldn't be in git)
static/uploads/*
!static/uploads/.gitkeep

# OS / editor cruft
.DS_Store
Thumbs.db
.vscode/
.idea/
*.swp

# Logs
*.log
"""
