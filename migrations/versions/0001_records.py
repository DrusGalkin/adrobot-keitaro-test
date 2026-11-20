from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None


def upgrade():
    op.create_table(
        "records",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("records")
