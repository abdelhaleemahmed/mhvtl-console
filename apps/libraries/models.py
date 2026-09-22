# apps/libraries/models.py
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
import hashlib


class LibraryBrand(models.Model):
    """Available library brands/vendors"""
    name = models.CharField(max_length=50, unique=True)
    display_name = models.CharField(max_length=100)
    color_code = models.CharField(max_length=7, default='#000000')  # Hex color
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.display_name


class LibraryModel(models.Model):
    """Library models for each brand"""
    brand = models.ForeignKey(LibraryBrand, on_delete=models.CASCADE, related_name='models')
    name = models.CharField(max_length=100)
    product_identification = models.CharField(max_length=100)
    default_revision = models.CharField(max_length=20, default='1068')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['brand__name', 'name']
        unique_together = ['brand', 'name']

    def __str__(self):
        return f"{self.brand.name} - {self.name}"


class Library(models.Model):
    """Main library configuration"""
    
    # Basic Library Info
    library_id = models.IntegerField(unique=True, validators=[MinValueValidator(1)])
    channel = models.IntegerField(validators=[MinValueValidator(0), MaxValueValidator(255)])
    target = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(255)])
    lun = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(255)])
    
    # Library Details
    brand = models.ForeignKey(LibraryBrand, on_delete=models.CASCADE)
    model = models.ForeignKey(LibraryModel, on_delete=models.CASCADE)
    vendor_identification = models.CharField(max_length=50)
    product_identification = models.CharField(max_length=100)
    product_revision = models.CharField(max_length=20, default='1068')
    unit_serial_number = models.CharField(max_length=50)
    naa = models.CharField(max_length=100, help_text="Network Address Authority")
    
    # Configuration
    home_directory = models.CharField(max_length=255, default='/opt/mhvtl')
    media_count = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(15000)])
    empty_slots = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    
    # Status
    is_active = models.BooleanField(default=True)
    is_standard_config = models.BooleanField(default=True, help_text="True for standard, False for custom")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ===== NEW DISCOVERY SERVICE FIELDS =====
    discovery_status = models.CharField(
        max_length=20,
        choices=[
            ('discovered', 'Discovered from MHVTL'),
            ('created', 'Created in Django'),
            ('synced', 'Synchronized'),
            ('conflict', 'Sync Conflict'),
        ],
        default='created',
        help_text="Track how this library was created or discovered"
    )
    mhvtl_id = models.CharField(
        max_length=50, 
        null=True, 
        blank=True,
        help_text="Original MHVTL library identifier from device.conf"
    )
    last_synced = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="Timestamp of last successful sync with MHVTL files"
    )
    sync_hash = models.CharField(
        max_length=64, 
        null=True, 
        blank=True,
        help_text="MD5 hash of configuration for change detection"
    )
    config_source = models.CharField(
        max_length=255, 
        null=True, 
        blank=True,
        help_text="Source file path (e.g., /etc/mhvtl/device.conf) or 'django-generated'"
    )
    discovery_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional metadata from discovery process"
    )

    class Meta:
        ordering = ['library_id']

    def __str__(self):
        return f"Library {self.library_id}: {self.brand.name} {self.model.name}"

    @property
    def total_slots(self):
        """Total number of slots (media + empty)"""
        return self.media_count + self.empty_slots

    @property
    def is_discovered(self):
        """Check if this library was discovered from MHVTL"""
        return self.discovery_status == 'discovered'

    @property
    def needs_sync(self):
        """Check if library needs synchronization"""
        return self.discovery_status in ['conflict', 'discovered'] or self.last_synced is None

    def calculate_sync_hash(self):
        """Calculate MD5 hash of current configuration for change detection"""
        config_data = f"{self.library_id}:{self.channel}:{self.target}:{self.lun}:" \
                     f"{self.vendor_identification}:{self.product_identification}:" \
                     f"{self.unit_serial_number}:{self.home_directory}:{self.media_count}"
        return hashlib.md5(config_data.encode()).hexdigest()

    def mark_synced(self):
        """Mark library as successfully synced"""
        self.last_synced = timezone.now()
        self.sync_hash = self.calculate_sync_hash()
        if self.discovery_status == 'conflict':
            self.discovery_status = 'synced'

    def detect_changes(self):
        """Detect if configuration has changed since last sync"""
        if not self.sync_hash:
            return True
        current_hash = self.calculate_sync_hash()
        return current_hash != self.sync_hash

    def clean(self):
        """Validate that total slots don't exceed 15000"""
        from django.core.exceptions import ValidationError
        if self.total_slots > 15000:
            raise ValidationError("Total number of library slots cannot exceed 15000")

    def save(self, *args, **kwargs):
        self.clean()
        # Auto-generate NAA if not provided
        if not self.naa and self.channel is not None and self.target is not None:
            self.naa = f"{self.library_id}:11:22:33:ab:{self.channel:02d}:{self.target:02d}:00"
        # Auto-generate serial number if not provided
        if not self.unit_serial_number:
            self.unit_serial_number = str(self.library_id + 80000000)
        
        # Set config_source for Django-created libraries
        if not self.config_source and self.discovery_status == 'created':
            self.config_source = 'django-generated'
            
        super().save(*args, **kwargs)


class Drive(models.Model):
    """Tape drives associated with libraries"""
    library = models.ForeignKey(Library, on_delete=models.CASCADE, related_name='drives')
    drive_id = models.IntegerField()
    channel = models.IntegerField()
    target = models.IntegerField()
    lun = models.IntegerField(default=0)
    
    vendor_identification = models.CharField(max_length=50)
    product_identification = models.CharField(max_length=100)
    product_revision = models.CharField(max_length=20)
    unit_serial_number = models.CharField(max_length=50)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # ===== NEW DISCOVERY SERVICE FIELDS =====
    discovery_status = models.CharField(
        max_length=20,
        choices=[
            ('discovered', 'Discovered from MHVTL'),
            ('created', 'Created in Django'),
            ('synced', 'Synchronized'),
        ],
        default='created'
    )
    mhvtl_id = models.CharField(
        max_length=50, 
        null=True, 
        blank=True,
        help_text="Original MHVTL drive identifier"
    )
    last_synced = models.DateTimeField(null=True, blank=True)
    config_source = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        ordering = ['library', 'drive_id']
        unique_together = ['library', 'drive_id']

    def __str__(self):
        return f"Drive {self.drive_id} (Library {self.library.library_id})"

    @property
    def is_discovered(self):
        """Check if this drive was discovered from MHVTL"""
        return self.discovery_status == 'discovered'


class MediaSlot(models.Model):
    """Media slots in libraries"""
    library = models.ForeignKey(Library, on_delete=models.CASCADE, related_name='slots')
    slot_number = models.IntegerField()
    is_empty = models.BooleanField(default=True)
    media_barcode = models.CharField(max_length=50, blank=True, null=True)
    media_type = models.CharField(max_length=20, blank=True, null=True)
    
    # Import/Export functionality
    is_import_export = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ===== NEW DISCOVERY SERVICE FIELDS =====
    discovery_status = models.CharField(
        max_length=20,
        choices=[
            ('discovered', 'Discovered from MHVTL'),
            ('created', 'Created in Django'),
            ('synced', 'Synchronized'),
        ],
        default='created'
    )
    last_synced = models.DateTimeField(null=True, blank=True)
    config_source = models.CharField(
        max_length=255, 
        null=True, 
        blank=True,
        help_text="Source file (e.g., library_contents.10)"
    )

    class Meta:
        ordering = ['library', 'slot_number']
        unique_together = ['library', 'slot_number']

    def __str__(self):
        status = "Empty" if self.is_empty else f"Media: {self.media_barcode}"
        return f"Slot {self.slot_number} ({status})"

    @property
    def is_discovered(self):
        """Check if this slot was discovered from MHVTL"""
        return self.discovery_status == 'discovered'


class LibraryOperation(models.Model):
    """Log of library operations"""
    OPERATION_CHOICES = [
        ('CREATE', 'Library Created'),
        ('UPDATE', 'Library Updated'),
        ('DELETE', 'Library Deleted'),
        ('IMPORT', 'Media Imported'),
        ('EXPORT', 'Media Exported'),
        ('MOVE', 'Media Moved'),
        ('MOUNT', 'Media Mounted'),
        ('UNMOUNT', 'Media Unmounted'),
        ('DISCOVER', 'Discovery Scan'),  # NEW
        ('SYNC', 'File Synchronization'),  # NEW
    ]
    
    library = models.ForeignKey(Library, on_delete=models.CASCADE, related_name='operations')
    operation = models.CharField(max_length=20, choices=OPERATION_CHOICES)
    description = models.TextField()
    user_session = models.CharField(max_length=100, help_text="Session ID of user")
    timestamp = models.DateTimeField(auto_now_add=True)
    
    # Optional references
    source_slot = models.IntegerField(null=True, blank=True)
    target_slot = models.IntegerField(null=True, blank=True)
    media_barcode = models.CharField(max_length=50, blank=True, null=True)

    # ===== NEW DISCOVERY SERVICE FIELDS =====
    discovery_session_id = models.CharField(
        max_length=100, 
        null=True, 
        blank=True,
        help_text="Discovery session identifier for tracking"
    )

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.operation}: {self.description} ({self.timestamp})"
