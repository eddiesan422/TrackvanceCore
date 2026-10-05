"""Add controlled governance and report executions without classifying legacy data."""
import sqlalchemy as sa
from alembic import op

revision = "0017_catalog_reports"
down_revision = "0016_acquisition_diagnostics"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('glossary_terms',
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('normalized_name', sa.String(length=160), nullable=False),
    sa.Column('definition', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'normalized_name')
    )
    with op.batch_alter_table('glossary_terms', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_glossary_terms_organization_id'), ['organization_id'], unique=False)

    op.create_table('macro_domains',
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('normalized_name', sa.String(length=160), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'normalized_name')
    )
    with op.batch_alter_table('macro_domains', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_macro_domains_organization_id'), ['organization_id'], unique=False)

    op.create_table('data_domains',
    sa.Column('macro_domain_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('normalized_name', sa.String(length=160), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['macro_domain_id'], ['macro_domains.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'macro_domain_id', 'normalized_name')
    )
    with op.batch_alter_table('data_domains', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_data_domains_macro_domain_id'), ['macro_domain_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_domains_organization_id'), ['organization_id'], unique=False)

    op.create_table('report_definitions',
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('owner_user_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'name')
    )
    with op.batch_alter_table('report_definitions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_report_definitions_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_definitions_owner_user_id'), ['owner_user_id'], unique=False)

    op.create_table('dataset_blocks',
    sa.Column('dataset_id', sa.String(length=64), nullable=False),
    sa.Column('scope', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.String(length=1000), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by_id', sa.String(length=64), nullable=False),
    sa.Column('released_by_id', sa.String(length=64), nullable=True),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('release_reason', sa.String(length=1000), nullable=True),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("scope IN ('REPORT','CONTENT')", name='ck_dataset_block_scope'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ),
    sa.ForeignKeyConstraint(['released_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('dataset_blocks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_dataset_blocks_dataset_id'), ['dataset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_dataset_blocks_organization_id'), ['organization_id'], unique=False)

    op.create_table('dataset_security_dependencies',
    sa.Column('dataset_id', sa.String(length=64), nullable=False),
    sa.Column('source_dataset_id', sa.String(length=64), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('released_by_id', sa.String(length=64), nullable=True),
    sa.Column('release_reason', sa.String(length=1000), nullable=True),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('dataset_id != source_dataset_id', name='ck_security_not_self'),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ),
    sa.ForeignKeyConstraint(['released_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['source_dataset_id'], ['datasets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_id', 'source_dataset_id')
    )
    with op.batch_alter_table('dataset_security_dependencies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_dataset_security_dependencies_dataset_id'), ['dataset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_dataset_security_dependencies_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_dataset_security_dependencies_source_dataset_id'), ['source_dataset_id'], unique=False)

    op.create_table('governance_history',
    sa.Column('dataset_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('actor_id', sa.String(length=64), nullable=True),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_id', 'version')
    )
    with op.batch_alter_table('governance_history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_governance_history_dataset_id'), ['dataset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_governance_history_organization_id'), ['organization_id'], unique=False)

    op.create_table('report_revisions',
    sa.Column('definition_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('draft', sa.JSON(), nullable=False),
    sa.Column('query_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by_user_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['definition_id'], ['report_definitions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('definition_id', 'version')
    )
    with op.batch_alter_table('report_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_report_revisions_definition_id'), ['definition_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_revisions_organization_id'), ['organization_id'], unique=False)

    op.create_table('column_documentation',
    sa.Column('dataset_version_id', sa.String(length=64), nullable=False),
    sa.Column('schema_hash', sa.String(length=64), nullable=False),
    sa.Column('column_name', sa.String(length=240), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['dataset_version_id'], ['dataset_versions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_version_id', 'column_name')
    )
    with op.batch_alter_table('column_documentation', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_column_documentation_dataset_version_id'), ['dataset_version_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_column_documentation_organization_id'), ['organization_id'], unique=False)

    op.create_table('glossary_associations',
    sa.Column('term_id', sa.String(length=64), nullable=False),
    sa.Column('dataset_id', sa.String(length=64), nullable=False),
    sa.Column('dataset_version_id', sa.String(length=64), nullable=True),
    sa.Column('column_name', sa.String(length=240), nullable=True),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ),
    sa.ForeignKeyConstraint(['dataset_version_id'], ['dataset_versions.id'], ),
    sa.ForeignKeyConstraint(['term_id'], ['glossary_terms.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('term_id', 'dataset_id', 'dataset_version_id', 'column_name')
    )
    with op.batch_alter_table('glossary_associations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_glossary_associations_dataset_id'), ['dataset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_glossary_associations_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_glossary_associations_term_id'), ['term_id'], unique=False)

    op.create_table('report_contexts',
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('revision_id', sa.String(length=64), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('integrity_hash', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['revision_id'], ['report_revisions.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('report_contexts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_report_contexts_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_contexts_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_contexts_user_id'), ['user_id'], unique=False)

    op.create_table('report_executions',
    sa.Column('context_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('profile', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('generation_status', sa.String(length=30), nullable=False),
    sa.Column('transmission_status', sa.String(length=30), nullable=False),
    sa.Column('idempotency_key', sa.String(length=100), nullable=True),
    sa.Column('request_hash', sa.String(length=64), nullable=True),
    sa.Column('publication', sa.JSON(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('progress_stage', sa.String(length=80), nullable=False),
    sa.Column('progress_percent', sa.Integer(), nullable=False),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('output_version_id', sa.String(length=64), nullable=True),
    sa.Column('error_code', sa.String(length=80), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['context_id'], ['report_contexts.id'], ),
    sa.ForeignKeyConstraint(['output_version_id'], ['dataset_versions.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'user_id', 'idempotency_key')
    )
    with op.batch_alter_table('report_executions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_report_executions_context_id'), ['context_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_executions_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_executions_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_executions_user_id'), ['user_id'], unique=False)

    op.create_table('strict_approvals',
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('input_version_id', sa.String(length=64), nullable=False),
    sa.Column('output_version_id', sa.String(length=64), nullable=False),
    sa.Column('contract_id', sa.String(length=64), nullable=False),
    sa.Column('contract_revision_id', sa.String(length=64), nullable=False),
    sa.Column('evidence_hash', sa.String(length=64), nullable=False),
    sa.Column('governance_snapshot', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('organization_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['contract_id'], ['configurations.id'], ),
    sa.ForeignKeyConstraint(['contract_revision_id'], ['configurations.id'], ),
    sa.ForeignKeyConstraint(['input_version_id'], ['dataset_versions.id'], ),
    sa.ForeignKeyConstraint(['output_version_id'], ['dataset_versions.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('output_version_id'),
    sa.UniqueConstraint('run_id')
    )
    with op.batch_alter_table('strict_approvals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_strict_approvals_contract_id'), ['contract_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_strict_approvals_input_version_id'), ['input_version_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_strict_approvals_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_strict_approvals_run_id'), ['run_id'], unique=False)

    with op.batch_alter_table('datasets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('macro_domain_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('domain_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('governance_version', sa.Integer(), server_default='1', nullable=False))
        batch_op.add_column(sa.Column('business_owner_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('steward_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('technical_custodian_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('information_classification', sa.String(length=40), server_default='UNKNOWN', nullable=False))
        batch_op.add_column(sa.Column('intake_input_dataset_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('intake_contract_id', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_datasets_domain_id'), ['domain_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_datasets_macro_domain_id'), ['macro_domain_id'], unique=False)
        batch_op.create_unique_constraint('uq_dataset_intake_identity', ['organization_id', 'intake_input_dataset_id', 'intake_contract_id'])
        batch_op.create_foreign_key('fk_datasets_datasets_input', 'datasets', ['intake_input_dataset_id'], ['id'])
        batch_op.create_foreign_key('fk_datasets_steward_id', 'users', ['steward_id'], ['id'])
        batch_op.create_foreign_key('fk_datasets_data_domains_domain', 'data_domains', ['domain_id'], ['id'])
        batch_op.create_foreign_key('fk_dataset_intake_contract', 'configurations', ['intake_contract_id'], ['id'], ondelete='SET NULL', use_alter=True)
        batch_op.create_foreign_key('fk_datasets_macro_domains_macro', 'macro_domains', ['macro_domain_id'], ['id'])
        batch_op.create_foreign_key('fk_datasets_technical_custodian_id', 'users', ['technical_custodian_id'], ['id'])
        batch_op.create_foreign_key('fk_datasets_business_owner_id', 'users', ['business_owner_id'], ['id'])

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_job_subject_lane', type_='check')
        batch_op.add_column(sa.Column('report_execution_id', sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint('uq_jobs_report_execution', ['report_execution_id'])
        batch_op.create_foreign_key('fk_jobs_report_execution', 'report_executions', ['report_execution_id'], ['id'])
        batch_op.create_check_constraint('ck_job_subject_lane', "(run_id IS NOT NULL AND acquisition_id IS NULL AND report_execution_id IS NULL AND lane IN ('DEFAULT','DELIVERY')) OR (run_id IS NULL AND acquisition_id IS NOT NULL AND report_execution_id IS NULL AND lane = 'ACQUISITION') OR (run_id IS NULL AND acquisition_id IS NULL AND report_execution_id IS NOT NULL AND lane = 'REPORT')")

    # ### end Alembic commands ###


def downgrade():
    connection = op.get_bind()
    tables = ("strict_approvals", "dataset_security_dependencies", "dataset_blocks", "glossary_associations",
              "column_documentation", "governance_history", "glossary_terms", "data_domains", "macro_domains",
              "report_executions", "report_contexts", "report_revisions", "report_definitions")
    for table in tables:
        if connection.execute(sa.text("SELECT COUNT(*) FROM " + table)).scalar():
            raise RuntimeError("La base contiene historia 0.8.0; recupera un backup verificado, sin descartarla mediante downgrade.")
    if connection.execute(sa.text("SELECT COUNT(*) FROM datasets WHERE macro_domain_id IS NOT NULL OR domain_id IS NOT NULL OR business_owner_id IS NOT NULL OR steward_id IS NOT NULL OR technical_custodian_id IS NOT NULL OR intake_input_dataset_id IS NOT NULL OR intake_contract_id IS NOT NULL OR governance_version != 1 OR information_classification != 'UNKNOWN'")).scalar():
        raise RuntimeError("La base contiene gobierno 0.8.0 que no puede perderse.")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_job_subject_lane", type_="check")
        batch.drop_constraint("uq_jobs_report_execution", type_="unique")
        batch.drop_constraint("fk_jobs_report_execution", type_="foreignkey")
        batch.drop_column("report_execution_id")
        batch.create_check_constraint("ck_job_subject_lane", "(run_id IS NOT NULL AND acquisition_id IS NULL AND lane IN ('DEFAULT','DELIVERY')) OR (run_id IS NULL AND acquisition_id IS NOT NULL AND lane = 'ACQUISITION')")
    with op.batch_alter_table("datasets") as batch:
        for name in ("fk_datasets_datasets_input", "fk_datasets_steward_id", "fk_datasets_data_domains_domain",
                     "fk_dataset_intake_contract", "fk_datasets_macro_domains_macro", "fk_datasets_technical_custodian_id",
                     "fk_datasets_business_owner_id"):
            batch.drop_constraint(name, type_="foreignkey")
        batch.drop_constraint("uq_dataset_intake_identity", type_="unique")
        batch.drop_index("ix_datasets_domain_id")
        batch.drop_index("ix_datasets_macro_domain_id")
        for name in ("macro_domain_id", "domain_id", "governance_version", "business_owner_id", "steward_id",
                     "technical_custodian_id", "information_classification", "intake_input_dataset_id", "intake_contract_id"):
            batch.drop_column(name)
    for table in tables:
        op.drop_table(table)
