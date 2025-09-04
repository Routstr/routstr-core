# Database Migrations

This guide covers database schema management using Alembic migrations in Routstr Core.

## Overview

Routstr uses Alembic for database migrations with these features:

- **Automatic migrations** on startup
- **Version control** for schema changes
- **Rollback capability** for safety
- **Support for multiple databases** (SQLite, PostgreSQL)

## Automatic Migrations

### Startup Behavior

Migrations run automatically when Routstr starts:

```python
# In routstr/core/main.py
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Running database migrations")
    run_migrations()  # Automatic migration
    await init_db()   # Initialize connection pool
    # ... rest of startup
```

This ensures:

- ✅ Database is always up-to-date
- ✅ No manual migration steps in production
- ✅ Zero-downtime deployments
- ✅ Backwards compatibility

### Migration Safety

Migrations are designed to be safe:

- Idempotent (can run multiple times)
- Non-destructive by default
- Tested before release
- Reversible when possible

## Creating Migrations

### Auto-generating from Models

After modifying SQLModel classes:

```bash
# Generate migration from model changes
make db-migrate

# You'll be prompted for a description
Enter migration message: Add user preferences table

# Review generated file
cat migrations/versions/xxxx_add_user_preferences_table.py
```

### Manual Migrations

For complex changes, create manually:

```bash
# Create empty migration
alembic revision -m "Complex data transformation"

# Edit the generated file
vim migrations/versions/xxxx_complex_data_transformation.py
```

### Migration Template

```python
"""Add user preferences table

Revision ID: a1b2c3d4e5f6
Revises: f6e5d4c3b2a1
Create Date: 2024-01-15 10:30:00.123456

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers
revision = 'a1b2c3d4e5f6'
down_revision = 'f6e5d4c3b2a1'
branch_labels = None
depends_on = None

def upgrade() -> None:
    """Apply migration."""
    # Create new table
    op.create_table(
        'userpreferences',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('api_key_id', sa.Integer(), nullable=False),
        sa.Column('theme', sa.String(), nullable=True),
        sa.Column('notifications_enabled', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['api_key_id'], ['apikey.id'], )
    )
    
    # Create index
    op.create_index(
        'ix_userpreferences_api_key_id',
        'userpreferences',
        ['api_key_id']
    )

def downgrade() -> None:
    """Revert migration."""
    op.drop_index('ix_userpreferences_api_key_id', table_name='userpreferences')
    op.drop_table('userpreferences')
```

## Common Migration Patterns

### Adding Columns

Add column with default value:

```python
def upgrade():
    # Add nullable column first
    op.add_column(
        'apikey',
        sa.Column('last_rotation', sa.DateTime(), nullable=True)
    )
    
    # Populate existing rows
    connection = op.get_bind()
    connection.execute(
        "UPDATE apikey SET last_rotation = created_at WHERE last_rotation IS NULL"
    )
    
    # Make non-nullable if needed
    op.alter_column('apikey', 'last_rotation', nullable=False)
```

### Renaming Columns

Safe column rename:

```python
def upgrade():
    # SQLite doesn't support ALTER COLUMN, so we need a workaround
    with op.batch_alter_table('apikey') as batch_op:
        batch_op.alter_column('old_name', new_column_name='new_name')
```

### Adding Indexes

Performance-improving indexes:

```python
def upgrade():
    # Single column index
    op.create_index(
        'ix_transaction_timestamp',
        'transaction',
        ['timestamp']
    )
    
    # Composite index
    op.create_index(
        'ix_transaction_key_time',
        'transaction',
        ['api_key_id', 'timestamp']
    )
    
    # Partial index (PostgreSQL only)
    op.create_index(
        'ix_apikey_active',
        'apikey',
        ['balance'],
        postgresql_where='balance > 0'
    )
```

### Data Migrations

Transform existing data:

```python
def upgrade():
    # Add new column
    op.add_column(
        'apikey',
        sa.Column('key_type', sa.String(), nullable=True)
    )
    
    # Migrate data
    connection = op.get_bind()
    result = connection.execute('SELECT id, metadata FROM apikey')
    
    for row in result:
        key_type = 'premium' if row.metadata.get('premium') else 'standard'
        connection.execute(
            f"UPDATE apikey SET key_type = '{key_type}' WHERE id = {row.id}"
        )
    
    # Make column non-nullable
    op.alter_column('apikey', 'key_type', nullable=False)
```

### Enum Types

Add enum column:

```python
from enum import Enum

class KeyStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    EXPIRED = "expired"

def upgrade():
    # Create enum type (PostgreSQL)
    key_status_enum = sa.Enum(KeyStatus, name='keystatus')
    key_status_enum.create(op.get_bind(), checkfirst=True)
    
    # Add column
    op.add_column(
        'apikey',
        sa.Column(
            'status',
            key_status_enum,
            nullable=False,
            server_default='active'
        )
    )
```

## Database-Specific Considerations

### SQLite Limitations

SQLite has limitations requiring workarounds:

```python
def upgrade():
    # SQLite doesn't support ALTER COLUMN directly
    # Use batch_alter_table for compatibility
    with op.batch_alter_table('apikey') as batch_op:
        batch_op.alter_column(
            'balance',
            type_=sa.BigInteger(),  # Change from Integer
            existing_type=sa.Integer()
        )
```

### PostgreSQL Features

Leverage PostgreSQL-specific features:

```python
def upgrade():
    # Use JSONB for better performance
    op.add_column(
        'apikey',
        sa.Column('metadata', sa.JSON().with_variant(
            sa.dialects.postgresql.JSONB(), 'postgresql'
        ))
    )
    
    # Add GIN index for JSONB queries
    op.create_index(
        'ix_apikey_metadata',
        'apikey',
        ['metadata'],
        postgresql_using='gin'
    )
    
    # Add check constraint
    op.create_check_constraint(
        'ck_apikey_balance_positive',
        'apikey',
        'balance >= 0'
    )
```

## Migration Commands

### Running Migrations

```bash
# Apply all pending migrations
make db-upgrade

# Upgrade to specific revision
alembic upgrade a1b2c3d4e5f6

# Upgrade one revision
alembic upgrade +1
```

### Checking Status

```bash
# Show current revision
make db-current
# Output: a1b2c3d4e5f6 (head)

# Show migration history
make db-history
# Output:
# a1b2c3d4e5f6 -> b2c3d4e5f6a7 (head), Add user preferences
# f6e5d4c3b2a1 -> a1b2c3d4e5f6, Add indexes
# e5d4c3b2a1f6 -> f6e5d4c3b2a1, Initial schema
```

### Rolling Back

```bash
# Rollback one migration
make db-downgrade

# Rollback to specific revision
alembic downgrade f6e5d4c3b2a1

# Rollback all (dangerous!)
alembic downgrade base
```

## Testing Migrations

### Unit Testing

Test migrations in isolation:

```python
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

def test_migration_add_user_preferences():
    """Test user preferences migration."""
    # Create test database
    engine = create_engine("sqlite:///:memory:")
    
    # Run migrations up to previous version
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
    command.upgrade(alembic_cfg, "f6e5d4c3b2a1")
    
    # Verify state before migration
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "userpreferences" not in tables
    
    # Run target migration
    command.upgrade(alembic_cfg, "a1b2c3d4e5f6")
    
    # Verify state after migration
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "userpreferences" in tables
    
    # Check columns
    columns = {col['name'] for col in inspector.get_columns('userpreferences')}
    assert columns == {'id', 'api_key_id', 'theme', 'notifications_enabled', 'created_at'}
    
    # Test downgrade
    command.downgrade(alembic_cfg, "f6e5d4c3b2a1")
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "userpreferences" not in tables
```

### Integration Testing

Test with real data:

```python
async def test_migration_with_data():
    """Test migration preserves existing data."""
    # Setup test database with data
    async with test_engine.begin() as conn:
        # Insert test data
        await conn.execute(
            "INSERT INTO apikey (key_hash, balance) VALUES ('test', 1000)"
        )
    
    # Run migration
    run_migrations()
    
    # Verify data integrity
    async with test_engine.connect() as conn:
        result = await conn.execute("SELECT * FROM apikey WHERE key_hash = 'test'")
        row = result.first()
        assert row.balance == 1000
        assert row.key_type == 'standard'  # New column with default
```

## Production Deployment

### Zero-Downtime Migrations

Strategy for seamless updates:

1. **Make migrations backwards compatible**

   ```python
   # Good: Add nullable column
   op.add_column('apikey', sa.Column('new_field', sa.String(), nullable=True))
   
   # Bad: Drop column immediately
   # op.drop_column('apikey', 'old_field')
   ```

2. **Deploy in phases**

   ```bash
   # Phase 1: Deploy code that works with both schemas
   # Phase 2: Run migration
   # Phase 3: Deploy code that requires new schema
   # Phase 4: Clean up deprecated columns
   ```

3. **Use feature flags**

   ```python
   if feature_enabled('use_new_schema'):
       # Use new column
       query = select(APIKey.new_field)
   else:
       # Use old column
       query = select(APIKey.old_field)
   ```

### Migration Monitoring

Track migration execution:

```python
# Add to migration
def upgrade():
    start_time = time.time()
    logger.info(f"Starting migration {revision}")
    
    try:
        # Migration logic here
        op.create_table(...)
        
        duration = time.time() - start_time
        logger.info(f"Migration {revision} completed in {duration:.2f}s")
    except Exception as e:
        logger.error(f"Migration {revision} failed: {e}")
        raise
```

### Backup Before Migration

Always backup before major changes:

```bash
#!/bin/bash
# backup_before_migration.sh

# Backup database
if [[ "$DATABASE_URL" == *"sqlite"* ]]; then
    cp database.db "backup_$(date +%Y%m%d_%H%M%S).db"
else
    pg_dump $DATABASE_URL > "backup_$(date +%Y%m%d_%H%M%S).sql"
fi

# Run migration
alembic upgrade head

# Verify
alembic current
```

## Troubleshooting

### Common Issues

**Migration Conflicts**

```bash
# Multiple heads detected
alembic heads
# a1b2c3d4e5f6 (head)
# b2c3d4e5f6a7 (head)

# Merge heads
alembic merge -m "Merge migrations" a1b2c3 b2c3d4
```

**Failed Migration**

```python
# Add rollback logic
def upgrade():
    try:
        op.create_table(...)
    except Exception as e:
        # Clean up partial changes
        op.drop_table('partial_table', checkfirst=True)
        raise

def downgrade():
    # Ensure clean rollback
    op.drop_table('new_table', checkfirst=True)
```

**Lock Timeout**

```python
# Add timeout handling
def upgrade():
    connection = op.get_bind()
    
    # Set timeout (PostgreSQL)
    connection.execute("SET lock_timeout = '10s'")
    
    try:
        op.add_column(...)
    except OperationalError as e:
        if 'lock timeout' in str(e):
            logger.error("Migration failed due to lock timeout")
            raise
```

### Recovery Procedures

If migration fails in production:

1. **Check current state**

   ```bash
   alembic current
   alembic history
   ```

2. **Manual rollback if needed**

   ```sql
   -- Check migration table
   SELECT * FROM alembic_version;
   
   -- Force version if necessary
   UPDATE alembic_version SET version_num = 'previous_version';
   ```

3. **Fix and retry**

   ```bash
   # Fix migration file
   vim migrations/versions/problematic_migration.py
   
   # Retry
   alembic upgrade head
   ```

## Best Practices

### Migration Guidelines

1. **Keep migrations small and focused**
   - One logical change per migration
   - Easier to review and rollback

2. **Test migrations thoroughly**
   - Test upgrade and downgrade
   - Test with production-like data
   - Test database-specific features

3. **Document breaking changes**

   ```python
   """BREAKING: Change balance column type
   
   This migration requires application update.
   Deploy order:
   1. Update application to handle both int and bigint
   2. Run this migration
   3. Update application to use only bigint
   """
   ```

4. **Make migrations idempotent**

   ```python
   def upgrade():
       # Check if column exists
       inspector = inspect(op.get_bind())
       columns = [col['name'] for col in inspector.get_columns('apikey')]
       
       if 'new_column' not in columns:
           op.add_column(
               'apikey',
               sa.Column('new_column', sa.String())
           )
   ```

### Performance Considerations

1. **Add indexes concurrently (PostgreSQL)**

   ```python
   def upgrade():
       # Create index without locking table
       op.create_index(
           'ix_large_table_column',
           'large_table',
           ['column'],
           postgresql_concurrently=True
       )
   ```

2. **Batch large updates**

   ```python
   def upgrade():
       connection = op.get_bind()
       
       # Process in batches
       batch_size = 1000
       offset = 0
       
       while True:
           result = connection.execute(
               f"UPDATE apikey SET processed = true "
               f"WHERE id IN (SELECT id FROM apikey WHERE processed = false LIMIT {batch_size})"
           )
           
           if result.rowcount == 0:
               break
               
           offset += batch_size
           time.sleep(0.1)  # Prevent overload
   ```

## Next Steps

- [Testing Guide](../contributing/testing.md) - Testing migrations
- [Deployment](../getting-started/docker.md) - Production deployment
