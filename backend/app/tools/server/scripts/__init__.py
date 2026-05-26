"""Script-management tools.

Importing this package registers each tool with the global registry.
Add new tools by creating a sibling module and importing it here.
"""

from app.tools.server.scripts import (  # noqa: F401
    create_folder,
    define_script,
    delete_folder,
    delete_script,
    edit_script,
    get_active_report,
    get_script,
    get_script_errors,
    list_data,
    list_scripts,
    move_to_folder,
    preview_data,
    run_script,
    screenshot_report,
    verify_script,
)
