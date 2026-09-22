# apps/libraries/urls.py - Complete Updated Version for Script Service Integration
from django.urls import path
from . import views
from . import ajax_views
from . import tape_operations_views
from . import console_views
from . import backup_restore_test_views
from . import iscsi_views

app_name = 'libraries'

# =============================================================================
# MAIN UI URLS - User-facing pages that render HTML templates
# =============================================================================
urlpatterns = [
    # Main dashboard
    path('', views.LibraryDashboardView.as_view(), name='dashboard'),
    
    # Setup workflows
    path('setup/', views.SetupChoiceView.as_view(), name='setup_choice'),
    path('setup/brand-selection/', views.BrandSelectionView.as_view(), name='brand_selection'),
    path('setup/brand/<str:brand_name>/', views.BrandConfigView.as_view(), name='brand_config'),
    path('setup/custom/', views.CustomSetupView.as_view(), name='custom_setup'),
    
    # Library management pages
    path('list/', views.LibraryListView.as_view(), name='list'),
    path('detail/<int:library_id>/', views.LibraryDetailView.as_view(), name='library_detail'),
    path('configure/<int:library_id>/', views.LibraryConfigureView.as_view(), name='library_configure'),
    path('monitor/<int:library_id>/', views.LibraryMonitorView.as_view(), name='library_monitor'),
    path('monitor/<int:library_id>/metrics/', views.LibraryMonitorMetricsView.as_view(), name='library_monitor_metrics'),
    path('remove/', views.LibraryRemoveView.as_view(), name='remove'),
    path('reset/', views.ResetDefaultView.as_view(), name='reset_default'),
    
    # Configuration file management - UPDATED: Now shows actual MHVTL files
    path('config-files/', views.ConfigFilesView.as_view(), name='config_files'),
    path('config-files/download/<str:filename>/', views.DownloadConfigView.as_view(), name='download_config'),
    path('config-files/download-all/', views.DownloadAllConfigsView.as_view(), name='download_all_configs'),
    
    # REMOVED: Django config generation endpoint
    # path('config-files/generate/', views.GenerateConfigView.as_view(), name='generate_config'),
    
    # NEW: MHVTL System Status and Management
    path('mhvtl-status/', views.MHVTLStatusView.as_view(), name='mhvtl_status'),

    # Database cleanup
    path('cleanup/', views.CleanupOrphanedView.as_view(), name='cleanup_orphaned'),

    # Console Module - System Information and Diagnostics
    path('console/', console_views.ConsoleDashboardView.as_view(), name='console_dashboard'),
    path('console/logs/', console_views.LogViewerView.as_view(), name='console_logs'),
    path('console/service/', console_views.ServiceStatusView.as_view(), name='console_service_status'),
    path('console/modules/', console_views.KernelModulesView.as_view(), name='console_kernel_modules'),
    path('console/disk/', console_views.DiskUsageView.as_view(), name='console_disk_usage'),
    path('console/devices/', console_views.ScsiDevicesView.as_view(), name='console_scsi_devices'),
]

# =============================================================================
# AJAX API URLS - Endpoints that return JSON data
# =============================================================================
urlpatterns += [
    # EXISTING: Library data APIs
    path('api/models/<int:brand_id>/', ajax_views.LibraryModelsAPIView.as_view(), name='api_library_models'),
    path('api/status/<int:library_id>/', ajax_views.LibraryStatusAPIView.as_view(), name='api_library_status'),
    path('api/status_detail/<int:library_id>/', ajax_views.library_status_detail, name='api_library_status_detail'),
    path('control/<int:library_id>/', views.LibraryControlView.as_view(), name='library_control'),
    
    # NEW: MHVTL Script Integration AJAX Endpoints
    path('ajax/create-library/', ajax_views.create_library_ajax, name='create_library_ajax'),
    path('ajax/update-library/<int:library_id>/', ajax_views.update_library_ajax, name='update_library_ajax'),
    path('ajax/delete-library/<int:library_id>/', ajax_views.delete_library_ajax, name='delete_library_ajax'),
    
    # NEW: MHVTL System Management AJAX
    path('ajax/mhvtl-status/', ajax_views.mhvtl_system_status_ajax, name='mhvtl_status_ajax'),
    path('ajax/dashboard-summary/', ajax_views.dashboard_summary_ajax, name='dashboard_summary_ajax'),
    path('ajax/validate-config/', ajax_views.validate_mhvtl_config_ajax, name='validate_config_ajax'),
    path('ajax/regenerate-configs/', ajax_views.regenerate_configs_ajax, name='regenerate_configs_ajax'),
    path('ajax/library-status-mhvtl/<int:library_id>/', ajax_views.get_library_status_mhvtl_ajax, name='library_status_mhvtl_ajax'),
    path('ajax/discovery-stats/', ajax_views.discovery_stats_ajax, name='discovery_stats_ajax'),
    path('ajax/preview-file/', ajax_views.preview_config_file_ajax, name='preview_config_file_ajax'),

    # Discovery Integration AJAX
    path('ajax/refresh-discovery/', ajax_views.refresh_discovery_ajax, name='refresh_discovery_ajax'),
    path('ajax/run-discovery/', ajax_views.run_discovery_ajax, name='run_discovery_ajax'),
    path('ajax/sync-library/<int:library_id>/', ajax_views.sync_library_ajax, name='sync_library_ajax'),
    path('ajax/cleanup-orphaned/', ajax_views.cleanup_orphaned_ajax, name='cleanup_orphaned_ajax'),

    # Console AJAX endpoints
    path('ajax/console-refresh/', console_views.console_refresh_ajax, name='console_refresh_ajax'),
    path('ajax/service-status/', console_views.service_status_ajax, name='service_status_ajax'),

    # UPDATED: Legacy AJAX endpoints (now use script service)
    path('ajax/export-library/<int:library_id>/', ajax_views.export_library_ajax, name='export_library_ajax'),
    path('ajax/validate-config/<int:library_id>/', ajax_views.validate_config_ajax, name='validate_config_ajax_legacy'),
    path('ajax/preview-config/<int:library_id>/', ajax_views.preview_config_ajax, name='preview_config_ajax'),
]

# =============================================================================
# TAPE OPERATIONS URLS - Operator panel for tape mount/unmount/move
# =============================================================================
urlpatterns += [
    # Operator Dashboard
    path('operator/', tape_operations_views.OperatorDashboardView.as_view(), name='operator_dashboard'),

    # Status Views
    path('operator/library-status/', tape_operations_views.LibraryStatusView.as_view(), name='library_status'),
    path('operator/library-status/<int:library_id>/', tape_operations_views.LibraryStatusView.as_view(), name='library_status_detail'),
    path('operator/drive-status/', tape_operations_views.DriveStatusView.as_view(), name='drive_status'),
    path('operator/drive-status/<int:drive_id>/', tape_operations_views.DriveStatusView.as_view(), name='drive_status_detail'),

    # Tape Movement Operations
    path('operator/mount/', tape_operations_views.MountTapeView.as_view(), name='mount_tape'),
    path('operator/unmount/', tape_operations_views.UnmountTapeView.as_view(), name='unmount_tape'),
    path('operator/move/', tape_operations_views.MoveTapeView.as_view(), name='move_tape'),

    # Library Control Operations
    path('operator/online/', tape_operations_views.LibraryOnlineView.as_view(), name='library_online'),
    path('operator/offline/', tape_operations_views.LibraryOfflineView.as_view(), name='library_offline'),
]

# =============================================================================
# TAPE OPERATIONS AJAX URLS - JSON endpoints for tape operations
# =============================================================================
urlpatterns += [
    # Status AJAX
    path('ajax/library-status/<int:library_id>/', tape_operations_views.library_status_ajax, name='library_status_ajax'),
    path('ajax/drive-status/<int:drive_id>/', tape_operations_views.drive_status_ajax, name='drive_status_ajax'),

    # Tape Operations AJAX
    path('ajax/mount-tape/', tape_operations_views.mount_tape_ajax, name='mount_tape_ajax'),
    path('ajax/unmount-tape/', tape_operations_views.unmount_tape_ajax, name='unmount_tape_ajax'),
    path('ajax/move-tape/', tape_operations_views.move_tape_ajax, name='move_tape_ajax'),

    # Device Discovery AJAX
    path('ajax/discover-devices/', tape_operations_views.discover_devices_ajax, name='discover_devices_ajax'),

    # LTO Compatibility AJAX (NEW)
    path('ajax/library-status-lto/<int:library_id>/', ajax_views.get_library_status_with_lto_ajax, name='library_status_lto_ajax'),
    path('ajax/check-mount-compatibility/<int:library_id>/', ajax_views.check_mount_compatibility_ajax, name='check_mount_compatibility_ajax'),
    path('ajax/drive-info/<int:library_id>/', ajax_views.get_drive_info_ajax, name='drive_info_ajax'),
]

# =============================================================================
# TAPE MANAGEMENT URLS - Tape create/delete/list
# =============================================================================
urlpatterns += [
    # Tape Inventory
    path('operator/tapes/', tape_operations_views.TapeListView.as_view(), name='tape_list'),
    path('operator/tapes/create/', tape_operations_views.CreateTapeView.as_view(), name='create_tape'),
    path('operator/tapes/create-bulk/', tape_operations_views.CreateTapesBulkView.as_view(), name='create_tapes_bulk'),
    path('operator/tapes/delete/', tape_operations_views.DeleteTapeView.as_view(), name='delete_tape'),
    path('operator/tapes/adopt/', tape_operations_views.AdoptTapeView.as_view(), name='adopt_tape'),

    # Drive Management
    path('operator/drives/', tape_operations_views.DriveListView.as_view(), name='drive_list'),
    path('operator/drives/add/', tape_operations_views.AddDriveView.as_view(), name='add_drive'),
    # A fragment of HTML, not JSON: the words are the service's, and the page
    # only puts them on screen. ?format=json answers the same data as numbers.
    path('activity/<int:library_id>/',
         tape_operations_views.library_activity, name='library_activity'),
    path('ajax/drive-placement/<int:library_id>/',
         tape_operations_views.drive_placement_ajax, name='drive_placement_ajax'),
    path('operator/drives/remove/', tape_operations_views.RemoveDriveView.as_view(), name='remove_drive'),
]

# =============================================================================
# TAPE/DRIVE MANAGEMENT AJAX URLS
# =============================================================================
urlpatterns += [
    # Tape Management AJAX
    path('ajax/tapes/<int:library_id>/', tape_operations_views.list_tapes_ajax, name='list_tapes_ajax'),
    path('ajax/tapes/create/', tape_operations_views.create_tape_ajax, name='create_tape_ajax'),
    path('ajax/tapes/delete/', tape_operations_views.delete_tape_ajax, name='delete_tape_ajax'),

    # Drive Management AJAX
    path('ajax/drives/', tape_operations_views.list_drives_ajax, name='list_drives_ajax'),
    path('ajax/drives/<int:library_id>/', tape_operations_views.list_drives_ajax, name='list_drives_library_ajax'),
    path('ajax/drives/add/', tape_operations_views.add_drive_ajax, name='add_drive_ajax'),
    path('ajax/drives/remove/', tape_operations_views.remove_drive_ajax, name='remove_drive_ajax'),

    # Barcode Utility AJAX
    path('ajax/density-suffix-mapping/', tape_operations_views.density_suffix_mapping_ajax, name='density_suffix_mapping_ajax'),
    path('ajax/validate-barcode/<int:library_id>/', tape_operations_views.validate_barcode_ajax, name='validate_barcode_ajax'),
    path('ajax/next-barcode/<int:library_id>/', tape_operations_views.next_barcode_ajax, name='next_barcode_ajax'),
    path('ajax/existing-barcodes/<int:library_id>/', tape_operations_views.existing_barcodes_ajax, name='existing_barcodes_ajax'),
    path('ajax/next-slot/<int:library_id>/', tape_operations_views.next_slot_ajax, name='next_slot_ajax'),
]

# =============================================================================
# BACKUP/RESTORE TEST URLS - End-to-end backup/restore verification testing
# =============================================================================
urlpatterns += [
    # Main backup/restore test interface
    path('operator/backup-restore-test/', backup_restore_test_views.BackupRestoreTestView.as_view(), name='backup_restore_test'),
    path('operator/backup-restore-test/results/', backup_restore_test_views.BackupRestoreTestResultsView.as_view(), name='backup_restore_test_results'),

    # AJAX endpoints for async test execution
    path('ajax/backup-restore-test/run/', backup_restore_test_views.BackupRestoreTestRunAsyncView.as_view(), name='backup_restore_test_run'),
    path('ajax/backup-restore-test/status/<str:test_id>/', backup_restore_test_views.BackupRestoreTestStatusView.as_view(), name='backup_restore_test_status'),
    path('ajax/backup-restore-test/library-tapes/<int:library_id>/', backup_restore_test_views.get_library_tapes_ajax, name='backup_restore_test_library_tapes'),
]

# =============================================================================
# ISCSI TARGET MANAGEMENT URLS - Export tape libraries over iSCSI
# =============================================================================
urlpatterns += [
    # iSCSI Dashboard
    path('iscsi/', iscsi_views.IscsiDashboardView.as_view(), name='iscsi_dashboard'),
    path('iscsi/guide/', iscsi_views.IscsiGuideView.as_view(), name='iscsi_guide'),

    # Target Management
    path('iscsi/targets/', iscsi_views.IscsiTargetsView.as_view(), name='iscsi_targets'),
    path('iscsi/targets/create/', iscsi_views.IscsiCreateTargetView.as_view(), name='iscsi_create_target'),
    path('iscsi/targets/<str:iqn>/', iscsi_views.IscsiTargetDetailView.as_view(), name='iscsi_target_detail'),
    path('iscsi/targets/<str:iqn>/attach/', iscsi_views.IscsiAttachView.as_view(), name='iscsi_attach'),

    # Backstore Management
    path('iscsi/backstores/', iscsi_views.IscsiBackstoresView.as_view(), name='iscsi_backstores'),

    # Quick Export Wizard
    path('iscsi/export/', iscsi_views.IscsiExportLibraryView.as_view(), name='iscsi_export_library'),

    # Service Control
    path('iscsi/service/', iscsi_views.IscsiServiceView.as_view(), name='iscsi_service'),
    path('iscsi/rebind/', iscsi_views.IscsiRebindView.as_view(), name='iscsi_rebind'),
    path('iscsi/record-binding/', iscsi_views.IscsiRecordBindingView.as_view(),
         name='iscsi_record_binding'),
]

# =============================================================================
# ISCSI AJAX URLS - JSON endpoints for iSCSI operations
# =============================================================================
urlpatterns += [
    # Status
    path('ajax/iscsi/status/', iscsi_views.IscsiStatusAjaxView.as_view(), name='iscsi_status_ajax'),

    # Target Management
    path('ajax/iscsi/target/create/', iscsi_views.CreateTargetAjaxView.as_view(), name='iscsi_create_target_ajax'),
    path('ajax/iscsi/target/delete/', iscsi_views.DeleteTargetAjaxView.as_view(), name='iscsi_delete_target_ajax'),

    # LUN Management
    path('ajax/iscsi/lun/add/', iscsi_views.AddLunAjaxView.as_view(), name='iscsi_add_lun_ajax'),
    path('ajax/iscsi/lun/delete/', iscsi_views.DeleteLunAjaxView.as_view(), name='iscsi_delete_lun_ajax'),

    # ACL Management
    path('ajax/iscsi/acl/add/', iscsi_views.AddAclAjaxView.as_view(), name='iscsi_add_acl_ajax'),
    path('ajax/iscsi/chap/', iscsi_views.ChapAjaxView.as_view(), name='iscsi_chap_ajax'),
    path('ajax/iscsi/acl/delete/', iscsi_views.DeleteAclAjaxView.as_view(), name='iscsi_delete_acl_ajax'),
    path('ajax/iscsi/acl/generate-node-acls/', iscsi_views.SetGenerateNodeAclsAjaxView.as_view(), name='iscsi_set_generate_node_acls_ajax'),

    # Portal Management
    path('ajax/iscsi/portal/add/', iscsi_views.AddPortalAjaxView.as_view(), name='iscsi_add_portal_ajax'),
    path('ajax/iscsi/portal/delete/', iscsi_views.DeletePortalAjaxView.as_view(), name='iscsi_delete_portal_ajax'),

    # Backstore Management
    path('ajax/iscsi/backstore/create/', iscsi_views.CreateBackstoreAjaxView.as_view(), name='iscsi_create_backstore_ajax'),
    path('ajax/iscsi/backstore/delete/', iscsi_views.DeleteBackstoreAjaxView.as_view(), name='iscsi_delete_backstore_ajax'),
]

# =============================================================================
# URL ORGANIZATION SUMMARY
# =============================================================================
"""
MAIN UI URLs (HTML Templates):
- / → Dashboard (LibraryDashboardView)
- /setup/ → Setup workflow (SetupChoiceView)
- /setup/brand-selection/ → Brand selection (BrandSelectionView)
- /setup/brand/<brand_name>/ → Brand configuration (BrandConfigView)
- /setup/custom/ → Custom setup (CustomSetupView)
- /list/ → Library list (LibraryListView)
- /detail/<library_id>/ → Library detail (LibraryDetailView)
- /configure/<library_id>/ → Library configuration (LibraryConfigureView)
- /monitor/<library_id>/ → Library monitoring (LibraryMonitorView)
- /remove/ → Remove libraries (LibraryRemoveView)
- /reset/ → Reset all libraries (ResetDefaultView)
- /config-files/ → View MHVTL config files (ConfigFilesView)
- /config-files/download/<filename>/ → Download config file (DownloadConfigView)
- /config-files/download-all/ → Download all configs as ZIP (DownloadAllConfigsView)
- /mhvtl-status/ → MHVTL system status (MHVTLStatusView) [NEW]

AJAX API URLs (JSON Responses):
Library Data APIs:
- /api/models/<brand_id>/ → Get models for brand (LibraryModelsAPIView)
- /api/status/<library_id>/ → Get library status (LibraryStatusAPIView)
- /api/status_detail/<library_id>/ → Detailed library status (library_status_detail)
- /api/control/<library_id>/ → Library control operations (LibraryControlView)

MHVTL Script Integration APIs [NEW]:
- /ajax/create-library/ → Create library via MHVTLLibraryService (create_library_ajax)
- /ajax/update-library/<library_id>/ → Update library via MHVTLLibraryService (update_library_ajax)
- /ajax/delete-library/<library_id>/ → Delete library via MHVTLLibraryService (delete_library_ajax)

MHVTL System Management APIs [NEW]:
- /ajax/mhvtl-status/ → Get MHVTL system status (mhvtl_system_status_ajax)
- /ajax/validate-config/ → Validate MHVTL configuration (validate_mhvtl_config_ajax)
- /ajax/regenerate-configs/ → Regenerate all configs via scripts (regenerate_configs_ajax)
- /ajax/library-status-mhvtl/<library_id>/ → Get library status from MHVTL (get_library_status_mhvtl_ajax)

Legacy AJAX APIs [UPDATED to use scripts]:
- /ajax/export-library/<library_id>/ → Export library to MHVTL (export_library_ajax)
- /ajax/validate-config/<library_id>/ → Validate library config (validate_config_ajax)
- /ajax/preview-config/<library_id>/ → Preview library config (preview_config_ajax)

REMOVED URLs:
- /config-files/generate/ → No longer needed with script service
"""

# =============================================================================
# MIGRATION NOTES
# =============================================================================
"""
Changes Made for Script Service Integration:

1. REMOVED:
   - generate_config URL (Django config generation no longer needed)

2. ADDED:
   - mhvtl-status/ → New MHVTL system status page
   - ajax/create-library/ → Script-based library creation
   - ajax/update-library/<id>/ → Script-based library updates
   - ajax/delete-library/<id>/ → Script-based library deletion
   - ajax/mhvtl-status/ → Real-time MHVTL system status
   - ajax/validate-config/ → MHVTL configuration validation
   - ajax/regenerate-configs/ → Script-based config regeneration
   - ajax/library-status-mhvtl/<id>/ → Direct MHVTL status queries

3. UPDATED:
   - config-files/ → Now shows actual MHVTL files instead of Django-generated
   - export-library/ → Now uses MHVTL scripts instead of Django config service
   - validate-config/ → Now includes MHVTL script validation
   - preview-config/ → Now shows actual MHVTL config format

4. PRESERVED:
   - All existing UI workflow URLs
   - All library management URLs
   - All brand/model selection URLs
   - All file download URLs
   - All existing AJAX data endpoints

Template JavaScript Updates Needed:
- Update regenerate button to call: {% url 'libraries:regenerate_configs_ajax' %}
- Health check button should call: {% url 'libraries:mhvtl_status_ajax' %}
- Library operations should use new script-based endpoints
- Config validation should use new MHVTL validation endpoints
"""
