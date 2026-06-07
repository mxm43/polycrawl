"""
测试脚本：使用指定cookies获取小红书用户主页信息
目标用户: 5eaec48e000000000100059f (https://www.xiaohongshu.com/user/profile/5eaec48e000000000100059f)
"""
from request.web.xhs_session import create_xhs_session
from loguru import logger
import asyncio
import json
import sys
import io

# 解决Windows终端GBK编码无法输出emoji的问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ============================================================
# 配置区 - 请根据实际情况修改
# ============================================================
PROXY = ""  # 代理地址，不需要则留空

# 提供的cookies
TEST_COOKIES = {
    "a1": "19e734f0010qf79b5h8pmnp4mnzncjy4mlhhkxdwz50000400826",
    "acw_tc": "0a00d08a17807826374621868e8e70452f285345f18501b111ff2e28c29c24",
    "webId": "ef4c30aafbdc40366fdea8f5448194d4",
    "web_session": "040069b5283f14f76a463ede11384b0114508c",
    "gid": "yjdWq28S4y62yjdWq4i881Cfy8AiWjD2xYU6xEuIU46Iux28ISVv4T888488YJK8iWq0J44i",
    "id_token": "VjEAADBmOC0KAoIzpduN1Rf+iPHf0va+spX869d7wgO289DGwiCuTBOYQ67tGfDrTxLR/cYi5ilvvcpbzj0euh81hBlWHz9Ojss0y23eC14bF4h9jsyuEs4SsiIGqK7XYamCdQXr",
    "xsecappid": "xhs-pc-web",
    "websectiga": "82e85efc5500b609ac1166aaf086ff8aa4261153a448ef0be5b17417e4512f28",
    "abRequestId": "05a9cfc7-afa1-58c0-8334-425a1aef9986",
    "ets": "1780051017619",
    "loadts": "1780782934618",
    "sec_poison_id": "92b57cb4-8e62-40ac-ab0c-97d87ad49de6",
    "unread": "{%22ub%22:%2269f4242b000000003701f286%22%2C%22ue%22:%2269f407a6000000003503246e%22%2C%22uc%22:17}",
    "webBuild": "6.15.2",
    "x-rednote-datactry": "CN",
    "x-rednote-holderctry": "CN"
}

TARGET_USER_ID = "5eaec48e000000000100059f"


async def test_user_profile():
    """测试获取用户主页完整信息"""
    logger.info("=" * 60)
    logger.info("开始测试小红书用户主页信息获取")
    logger.info(f"目标用户ID: {TARGET_USER_ID}")
    logger.info("=" * 60)

    # 创建session - 传入web_session确保登录态
    xhs_session = await create_xhs_session(
        proxy=PROXY,
        web_session=TEST_COOKIES["web_session"]
    )

    try:
        # ============================================================
        # 步骤1: 用提供的cookies覆盖自动生成的cookies
        # ============================================================
        logger.info("\n[步骤1] 更新cookies为指定值...")
        for key, value in TEST_COOKIES.items():
            xhs_session._session.cookie_jar.update_cookies({key: value})

        logger.success("Cookies更新完成，当前cookies:")
        for k, v in xhs_session.cookies.items():
            logger.info(f"  {k}: {v[:40] if len(v) > 40 else v}{'...' if len(v) > 40 else ''}")

        # ============================================================
        # 步骤2: 获取用户详细信息 (API)
        # ============================================================
        logger.info("\n[步骤2] 获取用户详细信息...")
        otherinfo_url = "https://edith.xiaohongshu.com/api/sns/web/v1/user/otherinfo"
        otherinfo_params = {"target_user_id": TARGET_USER_ID}

        res = await xhs_session.request(
            method="get",
            url=otherinfo_url,
            params=otherinfo_params
        )

        result = await res.json()
        logger.success("用户信息返回:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        # 检查数据完整性
        if result.get("success") and result.get("data"):
            data = result["data"]
            basic_info = data.get("basic_info", {})
            logger.info(f"  用户昵称: {basic_info.get('nickname', 'N/A')}")
            logger.info(f"  用户简介: {basic_info.get('desc', 'N/A')}")
            logger.info(f"  小红书号: {basic_info.get('red_id', 'N/A')}")
            logger.info(f"  性别: {'女' if basic_info.get('gender') == 1 else '男' if basic_info.get('gender') == 2 else '未知'}")
            logger.info(f"  IP属地: {basic_info.get('ip_location', 'N/A')}")

            # 从interactions列表中提取互动数据
            interactions = data.get("interactions", [])
            for item in interactions:
                logger.info(f"  {item.get('name')}: {item.get('count', 'N/A')}")

            # 标签信息
            tags = data.get("tags", [])
            for tag in tags:
                logger.info(f"  {tag.get('tagType')}: {tag.get('name', 'N/A')}")
        else:
            logger.warning("用户信息接口返回异常，请检查cookies是否有效")

        # ============================================================
        # 步骤3: 获取用户发布的笔记列表
        # ============================================================
        logger.info("\n[步骤3] 获取用户发布的笔记列表...")
        res2 = await xhs_session.apis.note.search_user_notes(
            user_id=TARGET_USER_ID,
            num=30,
            cursor=""
        )

        notes_result = await res2.json()
        logger.success("用户笔记列表返回:")
        print(json.dumps(notes_result, ensure_ascii=False, indent=2))

        if notes_result.get("success") and notes_result.get("data"):
            items = notes_result["data"].get("items", notes_result["data"].get("notes", []))
            if items:
                logger.info(f"  共获取到 {len(items)} 条笔记")
                for i, note in enumerate(items[:5], 1):
                    note_data = note.get("note_card", note)
                    logger.info(f"  笔记{i}: {note_data.get('display_title', note_data.get('title', '无标题'))}")
            else:
                logger.warning("笔记列表为空")
        else:
            logger.warning("笔记列表接口返回异常")

        # ============================================================
        # 步骤4: 获取用户收藏/点赞的笔记 (如接口有效)
        # ============================================================
        logger.info("\n[步骤4] 获取当前登录用户自身信息（验证登录态）...")
        try:
            res3 = await xhs_session.apis.auth.get_self_simple_info()
            self_info = await res3.json()
            logger.success("自身信息返回:")
            print(json.dumps(self_info, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning(f"获取自身信息失败（可能该cookies无登录态）: {e}")

        # ============================================================
        # 总结
        # ============================================================
        logger.info("\n" + "=" * 60)
        logger.info("测试完成 - 结果汇总")
        logger.info("=" * 60)
        logger.info(f"  目标用户ID: {TARGET_USER_ID}")
        logger.info(f"  用户信息: {'✅ 获取成功' if result.get('success') else '❌ 获取失败'}")
        logger.info(f"  笔记列表: {'✅ 获取成功' if notes_result.get('success') else '❌ 获取失败'}")

    except Exception as e:
        logger.error(f"测试过程中出现异常: {e}")
        logger.exception(e)
        sys.exit(1)
    finally:
        await xhs_session.close_session()
        logger.info("\nSession已关闭")


async def main():
    await test_user_profile()


if __name__ == "__main__":
    asyncio.run(main())
