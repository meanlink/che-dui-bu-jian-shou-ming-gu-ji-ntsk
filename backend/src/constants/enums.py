from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
class PeriodStatus(str, Enum):
    pending = 'pending'
    delivering = 'delivering'
    completed = 'completed'
    failed = 'failed'
    corrupted = 'corrupted'
