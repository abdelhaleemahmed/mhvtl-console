# apps/libraries/admin.py
from django.contrib import admin
from .models import LibraryBrand, LibraryModel, Library, Drive, MediaSlot, LibraryOperation


@admin.register(LibraryBrand)
class LibraryBrandAdmin(admin.ModelAdmin):
    list_display = ['name', 'display_name', 'color_code', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'display_name']
    list_editable = ['is_active']
    ordering = ['name']


@admin.register(LibraryModel)
class LibraryModelAdmin(admin.ModelAdmin):
    list_display = ['name', 'brand', 'product_identification', 'default_revision', 'is_active', 'created_at']
    list_filter = ['brand', 'is_active', 'created_at']
    search_fields = ['name', 'product_identification', 'brand__name']
    list_editable = ['is_active']
    ordering = ['brand__name', 'name']


class DriveInline(admin.TabularInline):
    model = Drive
    extra = 0
    fields = ['drive_id', 'channel', 'target', 'lun', 'vendor_identification', 
              'product_identification', 'is_active']


class MediaSlotInline(admin.TabularInline):
    model = MediaSlot
    extra = 0
    fields = ['slot_number', 'is_empty', 'media_barcode', 'media_type', 'is_import_export']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Library)
class LibraryAdmin(admin.ModelAdmin):
    list_display = ['library_id', 'brand', 'model', 'channel', 'target', 
                    'total_slots', 'is_active', 'created_at']
    list_filter = ['brand', 'is_active', 'is_standard_config', 'created_at']
    search_fields = ['library_id', 'vendor_identification', 'product_identification', 
                     'unit_serial_number']
    list_editable = ['is_active']
    ordering = ['library_id']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('library_id', 'channel', 'target', 'lun')
        }),
        ('Library Details', {
            'fields': ('brand', 'model', 'vendor_identification', 
                      'product_identification', 'product_revision', 
                      'unit_serial_number', 'naa')
        }),
        ('Configuration', {
            'fields': ('home_directory', 'media_count', 'empty_slots', 
                      'is_standard_config')
        }),
        ('Status', {
            'fields': ('is_active',)
        }),
    )
    
    readonly_fields = ['created_at', 'updated_at']
    inlines = [DriveInline, MediaSlotInline]

    def total_slots(self, obj):
        return obj.total_slots
    total_slots.short_description = 'Total Slots'


@admin.register(Drive)
class DriveAdmin(admin.ModelAdmin):
    list_display = ['drive_id', 'library', 'channel', 'target', 'lun', 
                    'vendor_identification', 'product_identification', 'is_active']
    list_filter = ['library', 'is_active', 'created_at']
    search_fields = ['drive_id', 'vendor_identification', 'product_identification', 
                     'unit_serial_number']
    list_editable = ['is_active']
    ordering = ['library__library_id', 'drive_id']


@admin.register(MediaSlot)
class MediaSlotAdmin(admin.ModelAdmin):
    list_display = ['library', 'slot_number', 'is_empty', 'media_barcode', 
                    'media_type', 'is_import_export', 'updated_at']
    list_filter = ['library', 'is_empty', 'is_import_export', 'media_type']
    search_fields = ['media_barcode', 'library__library_id']
    list_editable = ['is_empty', 'media_barcode', 'media_type']
    ordering = ['library__library_id', 'slot_number']


@admin.register(LibraryOperation)
class LibraryOperationAdmin(admin.ModelAdmin):
    list_display = ['library', 'operation', 'description', 'user_session', 'timestamp']
    list_filter = ['operation', 'timestamp', 'library']
    search_fields = ['description', 'user_session', 'media_barcode']
    readonly_fields = ['timestamp']
    ordering = ['-timestamp']
    
    def has_add_permission(self, request):
        return False  # Operations are created automatically
    
    def has_change_permission(self, request, obj=None):
        return False  # Operations shouldn't be modified