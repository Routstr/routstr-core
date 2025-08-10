"""
Settings management module for database-backed configuration.
Handles loading settings from database with environment variable fallback.
"""

import asyncio
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import Settings, create_session
from .logging import get_logger
from .settings_list import ENVIRONMENT_VARIABLES, EnvironmentVariableConfig

logger = get_logger(__name__)


class SettingsManager:
    """Manages application settings with database persistence and caching."""
    
    _cache: dict[str, Any] = {}
    _initialized: bool = False
    _init_lock = asyncio.Lock()
    
    @classmethod
    async def initialize(cls) -> None:
        """Initialize settings from environment variables if they don't exist in database."""
        async with cls._init_lock:
            if cls._initialized:
                return
                
            logger.info("Initializing settings from environment variables")
            
            try:
                async with create_session() as session:
                    # Get all existing settings
                    stmt = select(Settings)
                    result = await session.exec(stmt)  # type: ignore
                    existing_settings = result.all()
                    existing_keys = set()
                    
                    # Properly handle the results
                    for setting in existing_settings:
                        if hasattr(setting, 'key'):
                            existing_keys.add(setting.key)
                    
                    # Initialize missing settings from environment variables
                    new_settings = []
                    for key, config in ENVIRONMENT_VARIABLES.items():
                        if key not in existing_keys:
                            # Get value from environment or use default
                            default_value = config.get("default", "")
                            env_value = os.environ.get(key, default_value)
                            
                            # Skip empty required values during initialization
                            if config.get("required") and not env_value:
                                logger.warning(
                                    f"Required setting {key} not found in environment, "
                                    "will need to be configured"
                                )
                                continue
                            
                            setting = Settings.from_env_var(
                                key=key,
                                value=str(env_value),
                                value_type=config.get("type", "str"),
                                description=config.get("description"),
                            )
                            new_settings.append(setting)
                            logger.info(f"Initialized setting {key} from environment")
                    
                    if new_settings:
                        try:
                            session.add_all(new_settings)
                            await session.commit()
                            logger.info(f"Created {len(new_settings)} new settings from environment")
                        except IntegrityError:
                            # Another process already created the settings
                            await session.rollback()
                            logger.info("Settings already created by another process")
                    
                    # Update settings that haven't been manually changed
                    updated_count = 0
                    for key, config in ENVIRONMENT_VARIABLES.items():
                        if key in existing_keys:
                            # Get current environment value
                            env_value = os.environ.get(key)
                            if env_value is None:
                                continue
                            
                            # Find the existing setting
                            stmt = select(Settings).where(Settings.key == key)  # type: ignore
                            result = await session.exec(stmt)  # type: ignore
                            setting = result.first()
                            
                            if setting and hasattr(setting, 'is_manually_changed') and not setting.is_manually_changed:
                                # Check if environment value differs from database value
                                if hasattr(setting, 'value') and str(env_value) != setting.value:
                                    setting.value = str(env_value)
                                    setting.updated_at = datetime.utcnow()
                                    updated_count += 1
                                    logger.info(
                                        f"Updated setting {key} from environment "
                                        f"(not manually changed)"
                                    )
                    
                    if updated_count > 0:
                        await session.commit()
                        logger.info(f"Updated {updated_count} settings from environment")
                    
                    # Load all settings into cache
                    await cls.reload_cache()
                    
            except Exception as e:
                logger.error(f"Error initializing settings: {e}")
                raise
                
            cls._initialized = True
    
    @classmethod
    async def reload_cache(cls) -> None:
        """Reload all settings from database into cache."""
        async with create_session() as session:
            stmt = select(Settings)
            result = await session.exec(stmt)  # type: ignore
            settings = result.all()
            cls._cache = {}
            
            for setting in settings:
                if hasattr(setting, 'key') and hasattr(setting, 'get_typed_value'):
                    try:
                        cls._cache[setting.key] = setting.get_typed_value()
                    except Exception as e:
                        logger.error(f"Error loading setting {setting.key}: {e}")
                        
            logger.debug(f"Loaded {len(cls._cache)} settings into cache")
    
    @classmethod
    async def get(cls, key: str, default: Any = None) -> Any:
        """Get a setting value from cache or database."""
        if not cls._initialized:
            await cls.initialize()
        
        # Check cache first
        if key in cls._cache:
            return cls._cache[key]
        
        # If not in cache, try to load from database
        try:
            async with create_session() as session:
                stmt = select(Settings).where(Settings.key == key)  # type: ignore
                result = await session.exec(stmt)  # type: ignore
                setting = result.first()
                
                if setting and hasattr(setting, 'get_typed_value'):
                    value = setting.get_typed_value()
                    cls._cache[key] = value
                    return value
        except Exception as e:
            logger.error(f"Error getting setting {key}: {e}")
        
        # Return default if not found
        return default
    
    @classmethod
    async def set(
        cls,
        key: str,
        value: Any,
        mark_as_manually_changed: bool = True
    ) -> None:
        """Set a setting value in database and cache."""
        if not cls._initialized:
            await cls.initialize()
        
        async with create_session() as session:
            stmt = select(Settings).where(Settings.key == key)  # type: ignore
            result = await session.exec(stmt)  # type: ignore
            setting = result.first()
            
            if setting:
                setting.value = str(value)
                setting.updated_at = datetime.utcnow()
                if mark_as_manually_changed:
                    setting.is_manually_changed = True
            else:
                # Create new setting
                config: EnvironmentVariableConfig = ENVIRONMENT_VARIABLES.get(key, {"default": "", "description": "", "type": "str", "locations": []})  # type: ignore
                setting = Settings(
                    key=key,
                    value=str(value),
                    value_type=config.get("type", "str"),
                    description=config.get("description"),
                    is_manually_changed=mark_as_manually_changed,
                )
                session.add(setting)
            
            await session.commit()
            
            # Update cache
            value_type = getattr(setting, 'value_type', 'str')
            if value_type == "int":
                cls._cache[key] = int(value)
            elif value_type == "float":
                cls._cache[key] = float(value)
            elif value_type == "bool":
                cls._cache[key] = str(value).lower() in ("true", "1", "yes", "on")
            else:
                cls._cache[key] = str(value)
            
            logger.info(f"Updated setting {key} = {value}")
    
    @classmethod
    async def get_all(cls) -> dict[str, Any]:
        """Get all settings as a dictionary."""
        if not cls._initialized:
            await cls.initialize()
        
        return cls._cache.copy()
    
    @classmethod
    async def reset(cls, key: str) -> None:
        """Reset a setting to its environment variable value."""
        env_value = os.environ.get(key)
        if env_value is None:
            config: EnvironmentVariableConfig = ENVIRONMENT_VARIABLES.get(key, {"default": "", "description": "", "type": "str", "locations": []})  # type: ignore
            default_value = config.get("default", "")
            env_value = default_value
        
        await cls.set(key, env_value, mark_as_manually_changed=False)
        logger.info(f"Reset setting {key} to environment value: {env_value}")
    
    @classmethod
    def reset_for_testing(cls) -> None:
        """Reset the settings manager state for testing."""
        cls._cache = {}
        cls._initialized = False


# Convenience function for backward compatibility
async def get_setting(key: str, default: Any = None) -> Any:
    """Get a setting value (convenience function)."""
    return await SettingsManager.get(key, default)
