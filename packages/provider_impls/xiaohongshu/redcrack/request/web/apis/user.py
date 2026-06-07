
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from request.web.xhs_session import XHS_Session  # 仅类型检查时导入




class User:
    def __init__(self, session: "XHS_Session"):
        self.session = session  # 保存会话引用

    # 关注用户
    def follow_user(self, target_user_id: str) :
        """关注用户

        Args:
            target_user_id: 目标用户ID

        Returns:
            点赞结果
        """
        url = "https://edith.xiaohongshu.com/api/sns/web/v1/user/follow"
        data = {
            "target_user_id": target_user_id
        }
        return self.session.request(method="post", url=url, data=data)


