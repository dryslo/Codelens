"""Baseline-схема.

Полная схема приложения одной ревизией: пользователи/credentials/identity (OIDC)/refresh-токены,
чаты/сообщения, реестр проиндексированных файлов. Стартовая точка миграций для текущей версии.

Revision ID: fff822192562
Revises:
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'fff822192562'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('chats',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chats_user_id'), 'chats', ['user_id'], unique=False)
    op.create_table('indexed_files',
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('file', sa.String(), nullable=False),
    sa.Column('hash', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('source', 'file')
    )
    op.create_table('users',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('login', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('login')
    )
    op.create_table('credentials',
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('password_hash', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('identities',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('provider', sa.String(), nullable=False),
    sa.Column('subject', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'subject', name='uq_identity_provider_subject')
    )
    op.create_index(op.f('ix_identities_user_id'), 'identities', ['user_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('chat_id', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('retrieved_ids', sa.Text(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.Column('mode', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messages_chat_id'), 'messages', ['chat_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_refresh_tokens_token_hash'), 'refresh_tokens', ['token_hash'], unique=False)
    op.create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_refresh_tokens_user_id'), table_name='refresh_tokens')
    op.drop_index(op.f('ix_refresh_tokens_token_hash'), table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index(op.f('ix_messages_chat_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_identities_user_id'), table_name='identities')
    op.drop_table('identities')
    op.drop_table('credentials')
    op.drop_table('users')
    op.drop_table('indexed_files')
    op.drop_index(op.f('ix_chats_user_id'), table_name='chats')
    op.drop_table('chats')
