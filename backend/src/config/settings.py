import os
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./local.db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.environ.get('JWT_SECRET', 'local-development-change-before-deploy')
ARTIFACT_DIR = os.getenv('ARTIFACT_DIR', '/artifacts')
# How long one worker may hold a claimed period before another worker may
# take it over (a crashed/restarted worker must not strand a half-written issue).
PERIOD_CLAIM_TTL = float(os.getenv('PERIOD_CLAIM_TTL', '180'))
# Upper bound of catch-up issues generated per report per tick, so a long
# pause cannot block the loop while still eventually backfilling every issue.
CATCHUP_BATCH = int(os.getenv('CATCHUP_BATCH', '50'))
