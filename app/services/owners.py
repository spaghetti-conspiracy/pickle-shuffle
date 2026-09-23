"""団体と管理者。

データはすべて団体（``owners``）に紐づく。いまは1つしか作らないが、
管理者を複数登録して認証を本格化するときに**テーブルを変えずに済む**よう、
最初から構造として持っている。

絞り込みの入口はここ1つにする。サービス層は ``owner`` を引数で受け取り、
問い合わせには必ず ``owner_id`` の条件を入れる。認証を足すときは
``current_owner`` の中身を差し替えるだけで済む。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_password
from app.config import settings
from app.errors import NotFoundError, UnauthorizedError
from app.models import Admin, Owner, OwnerAdmin

DEFAULT_OWNER_NAME = "既定の団体"
BOOTSTRAP_LOGIN = "admin"


def ensure_bootstrap(db: Session) -> Owner:
    """既定の団体と、環境変数から作る固定の管理者を用意する。

    起動のたびに呼ぶ。**``is_bootstrap`` の管理者に限り、毎回
    ``ADMIN_PASSWORD`` からハッシュを作り直す。** 環境変数を変えたら
    パスワードが変わり、DB を作り直さなくてよい。本物の管理者を登録した
    あとは、この行を消してかまわない。
    """
    owner = db.scalars(select(Owner).order_by(Owner.id)).first()
    if owner is None:
        owner = Owner(name=DEFAULT_OWNER_NAME)
        db.add(owner)
        db.flush()

    admin = db.scalars(
        select(Admin).where(Admin.login == BOOTSTRAP_LOGIN, Admin.is_bootstrap.is_(True))
    ).first()
    if admin is None:
        admin = Admin(
            login=BOOTSTRAP_LOGIN,
            password_hash=hash_password(settings.admin_password),
            is_bootstrap=True,
        )
        db.add(admin)
        db.flush()
    elif not verify_password(settings.admin_password, admin.password_hash):
        # 環境変数が変わった。古いクッキーはハッシュごと変わるので無効になる。
        admin.password_hash = hash_password(settings.admin_password)

    link = db.get(OwnerAdmin, (owner.id, admin.id))
    if link is None:
        db.add(OwnerAdmin(owner_id=owner.id, admin_id=admin.id))
    try:
        db.commit()
    except IntegrityError:
        # サーバーレスでは複数のインスタンスが同時に起動して、同じ行を
        # 作ろうとすることがある。先に誰かが作っていれば、それでよい。
        db.rollback()
        return current_owner(db)
    return owner


def authenticate(db: Session, password: str) -> Admin:
    """合言葉を確かめる。合っていれば、その管理者を返す。"""
    for admin in db.scalars(select(Admin).order_by(Admin.id)):
        if verify_password(password, admin.password_hash):
            return admin
    raise UnauthorizedError("合言葉が違います")


def get_admin(db: Session, admin_id: int) -> Admin | None:
    return db.get(Admin, admin_id)


def current_owner(db: Session, admin: Admin | None = None) -> Owner:
    """いま見ている団体。

    管理者が分かればその人の団体。分からないのはテストや移行のときだけで、
    そのときは既定の団体（1つしかない）を返す。

    **管理者が分かっているのに団体が無ければ、既定へは落とさない。**
    団体が増えた瞬間に「よその団体が見える」へ化ける既定値になるため。
    """
    if admin is not None:
        owner = db.scalars(
            select(Owner)
            .join(OwnerAdmin, OwnerAdmin.owner_id == Owner.id)
            .where(OwnerAdmin.admin_id == admin.id)
            .order_by(Owner.id)
        ).first()
        if owner is None:
            raise NotFoundError("この管理者に団体が紐づいていません")
        return owner
    owner = db.scalars(select(Owner).order_by(Owner.id)).first()
    if owner is None:
        raise NotFoundError("団体が登録されていません")
    return owner
