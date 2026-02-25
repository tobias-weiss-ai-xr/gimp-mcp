## GEGL Filter System - Release Notes

### New Features
- Discovery: `list_gegl_filters()` to enumerate available GEGL filters, including filter id, name, and a short description.
- Application: `apply_gegl_filter()` to apply a selected GEGL filter to the active image, with a flexible parameter map for filter-specific settings.
- Preview: `preview_gegl_filter()` to generate non-destructive previews before applying changes to the image.
- Error Handling: improved error reporting with descriptive messages and fallback behavior to preserve user data when a filter fails.

### Known Issues
- Some filters may require specific image formats or color spaces; unsupported configurations may raise errors.
- Preview rendering can be memory-intensive for large images or high-resolution previews.
- Certain plugins or custom GEGL nodes may be unavailable on minimal installations, affecting discovery.
- Error messages may still surface non-user-friendly details in edge cases; logs are recommended for advanced troubleshooting.

### Upgrade Instructions
- Back up current projects and user data before upgrading.
- Replace legacy GEGL filter calls with new APIs: list_gegl_filters, apply_gegl_filter, preview_gegl_filter.
- Update parameter schemas to match the new filter parameter map.
- Rebuild and restart the GIMP MCP server or GIMP plugin to ensure the new API is loaded.
- Review any custom scripts for compatibility with the new API surface and adjust accordingly.
