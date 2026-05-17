# HiveDeploy — thin entrypoint
# Bootstrap (app init, DB migrations, template setup) lives in bootstrap.py.
# All route handlers are in routes_*.py with their own APIRouters.

from .bootstrap import app

from .routes_auth import router as auth_router
from .routes_user import router as user_router
from .routes_instances import router as instances_router
from .routes_files import router as files_router
from .routes_admin import router as admin_router
from .routes_invites import router as invites_router
from .routes_nodes import router as nodes_router
from .routes_terminal import router as terminal_router

app.include_router(auth_router)
app.include_router(user_router)
app.include_router(instances_router)
app.include_router(files_router)
app.include_router(admin_router)
app.include_router(invites_router)
app.include_router(nodes_router)
app.include_router(terminal_router)
