from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config.models import CreatorsFile
from packages.core.db.models import Account, Artifact, Creator


async def sync_creators_to_db(
    session_factory: async_sessionmaker[AsyncSession],
    creators_file: CreatorsFile,
) -> None:
    async with session_factory() as session:
        creator_id_by_key: dict[str, int] = {}

        for item in creators_file.creators:
            if not item.creator_key:
                continue

            existing_creator = await _get_creator_by_key(session, item.creator_key)
            if existing_creator is None:
                creator = Creator(
                    creator_key=item.creator_key,
                    display_name=item.display_name,
                )
                session.add(creator)
                await session.flush()
                creator_id_by_key[item.creator_key] = creator.id
            else:
                existing_creator.display_name = item.display_name
                existing_creator.updated_at = datetime.utcnow()
                creator_id_by_key[item.creator_key] = existing_creator.id

        config_creator_keys = {
            item.creator_key
            for item in creators_file.creators
            if item.creator_key
        }

        await _delete_missing_creators(session, config_creator_keys)

        for item in creators_file.creators:
            if not item.creator_key:
                continue
            creator_id = creator_id_by_key[item.creator_key]

            keep_account_keys: set[tuple[str, str, str]] = set()
            for account_item in item.accounts:
                for url_entry in account_item.normalized_urls():
                    account_url = url_entry.url.strip().rstrip("?")
                    if not account_url:
                        continue
                    account_key = (account_item.platform, account_item.type, account_url)
                    keep_account_keys.add(account_key)

                    existing_account = await _get_account(session, creator_id, *account_key)
                    if existing_account is None:
                        session.add(
                            Account(
                                creator_id=creator_id,
                                platform=account_item.platform,
                                account_type=account_item.type,
                                account_url=account_url,
                                account_alias=account_item.account_alias,
                                scheduled=url_entry.enabled,
                            )
                        )
                    else:
                        existing_account.account_alias = account_item.account_alias
                        existing_account.scheduled = url_entry.enabled
                        existing_account.updated_at = datetime.utcnow()

            await _delete_missing_accounts(session, creator_id, keep_account_keys)

        await session.commit()


async def _get_creator_by_key(session: AsyncSession, creator_key: str) -> Creator | None:
    result = await session.execute(select(Creator).where(Creator.creator_key == creator_key))
    return result.scalars().first()


async def _get_account(
    session: AsyncSession,
    creator_id: int,
    platform: str,
    account_type: str,
    account_url: str,
) -> Account | None:
    result = await session.execute(
        select(Account).where(
            and_(
                Account.creator_id == creator_id,
                Account.platform == platform,
                Account.account_type == account_type,
                Account.account_url == account_url,
            )
        )
    )
    return result.scalars().first()


async def _delete_missing_creators(session: AsyncSession, keep_keys: set[str]) -> None:
    """删除配置中不再存在的创作者，但跳过仍有 artifact 的创作者。"""
    if not keep_keys:
        return  # 安全保护：不传 keep_keys 时不删除任何 creator

    # 查出所有待删除的 creator
    result = await session.execute(
        select(Creator).where(Creator.creator_key.not_in(keep_keys))
    )
    for creator in result.scalars().all():
        # 检查是否有 artifact（通过 accounts 关联）
        cnt = await session.scalar(
            select(func.count())
            .select_from(Artifact)
            .join(Account, Artifact.account_id == Account.id)
            .where(Account.creator_id == creator.id)
        )
        if cnt and cnt > 0:
            continue  # 有历史数据，保留
        await session.delete(creator)


async def _delete_missing_accounts(
    session: AsyncSession,
    creator_id: int,
    keep_account_keys: set[tuple[str, str, str]],
) -> None:
    result = await session.execute(select(Account).where(Account.creator_id == creator_id))
    existing_accounts = result.scalars().all()

    for account in existing_accounts:
        key = (account.platform, account.account_type, account.account_url)
        if key not in keep_account_keys:
            # ── 安全检查：如有 artifact 关联，不能删除 ──
            cnt = await session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.account_id == account.id)
            )
            if cnt and cnt > 0:
                continue  # 有历史数据，保留该 account
            await session.delete(account)
