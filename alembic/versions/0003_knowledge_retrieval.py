"""knowledge retrieval persistence

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_knowledge_documents_created_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "created_by",
            "idempotency_key",
            name="uq_knowledge_documents_created_by_idempotency_key",
        ),
    )
    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "accepted",
                "processing",
                "active",
                "failed",
                "disabled",
                name="knowledge_version_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parser_version", sa.String(length=64), nullable=True),
        sa.Column("chunker_version", sa.String(length=64), nullable=True),
        sa.Column("embedding_version", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('accepted', 'processing', 'active', 'failed', 'disabled')",
            name="ck_knowledge_document_versions_status",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="ck_knowledge_document_versions_attempt_count",
        ),
        sa.CheckConstraint(
            "version_number >= 1",
            name="ck_knowledge_document_versions_version_number",
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_knowledge_document_versions_lease_state",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name="fk_knowledge_document_versions_document_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "version_number",
            name="uq_knowledge_document_versions_document_id_version_number",
        ),
        sa.UniqueConstraint(
            "document_id",
            "sha256",
            name="uq_knowledge_document_versions_document_id_sha256",
        ),
        sa.UniqueConstraint(
            "document_id",
            "idempotency_key",
            name="uq_knowledge_document_versions_document_id_idempotency_key",
        ),
    )
    op.create_foreign_key(
        "fk_knowledge_documents_current_version_id",
        "knowledge_documents",
        "knowledge_document_versions",
        ["current_version_id"],
        ["id"],
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_text", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunks_chunk_index"),
        sa.CheckConstraint("token_count > 0", name="ck_knowledge_chunks_token_count"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["knowledge_document_versions.id"],
            name="fk_knowledge_chunks_version_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_id",
            "chunk_index",
            name="uq_knowledge_chunks_version_id_chunk_index",
        ),
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_knowledge_documents_current_version_id",
        "knowledge_documents",
        type_="foreignkey",
    )
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_document_versions")
    op.drop_table("knowledge_documents")
