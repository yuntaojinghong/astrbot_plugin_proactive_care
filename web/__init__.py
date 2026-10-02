"""面板后端：路由与业务逻辑。"""

from .api import PLUGIN_NAME, WebController
from .service import PanelService

__all__ = ["WebController", "PanelService", "PLUGIN_NAME"]
