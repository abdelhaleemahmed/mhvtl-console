# apps/libraries/forms.py
from django import forms
from django.core.exceptions import ValidationError
from .models import Library, LibraryBrand, LibraryModel, Drive


class LibraryCreateForm(forms.ModelForm):
    """Form for creating a new library"""
    
    class Meta:
        model = Library
        fields = [
            'library_id', 'channel', 'target', 'lun',
            'brand', 'model', 'vendor_identification',
            'product_identification', 'product_revision',
            'unit_serial_number', 'media_count', 'empty_slots',
            'home_directory', 'is_standard_config'
        ]
        
        widgets = {
            'library_id': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '1',
                'max': '999',
                'required': True
            }),
            'channel': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '255',
                'required': True
            }),
            'target': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '255',
                'value': '0'
            }),
            'lun': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '255',
                'value': '0'
            }),
            'brand': forms.Select(attrs={'class': 'form-control'}),
            'model': forms.Select(attrs={'class': 'form-control'}),
            'vendor_identification': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '50'
            }),
            'product_identification': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '100'
            }),
            'product_revision': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '20',
                'value': '1068'
            }),
            'unit_serial_number': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '50'
            }),
            'media_count': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '15000',
                'value': '0'
            }),
            'empty_slots': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '15000',
                'value': '0'
            }),
            'home_directory': forms.TextInput(attrs={
                'class': 'form-control',
                'value': '/opt/mhvtl'
            }),
            'is_standard_config': forms.CheckboxInput(attrs={
                'class': 'form-check-input'
            })
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Set up brand choices
        self.fields['brand'].queryset = LibraryBrand.objects.filter(is_active=True)
        self.fields['model'].queryset = LibraryModel.objects.none()
        
        # If brand is selected, filter models
        if 'brand' in self.data:
            try:
                brand_id = int(self.data.get('brand'))
                self.fields['model'].queryset = LibraryModel.objects.filter(
                    brand_id=brand_id, is_active=True
                ).order_by('name')
            except (ValueError, TypeError):
                pass
        elif self.instance.pk:
            self.fields['model'].queryset = self.instance.brand.models.filter(is_active=True)

    def clean_library_id(self):
        library_id = self.cleaned_data['library_id']
        
        # Check if library ID already exists
        if Library.objects.filter(library_id=library_id, is_active=True).exists():
            raise ValidationError(f'Library ID {library_id} already exists.')
        
        return library_id

    def clean(self):
        cleaned_data = super().clean()
        media_count = cleaned_data.get('media_count', 0)
        empty_slots = cleaned_data.get('empty_slots', 0)
        
        # Validate total slots don't exceed 15000
        total_slots = media_count + empty_slots
        if total_slots > 15000:
            raise ValidationError(
                'Total number of library slots (media + empty) cannot exceed 15000. '
                f'Current total: {total_slots}'
            )
        
        return cleaned_data


class LibraryConfigForm(forms.Form):
    """Form for configuring a library with brand-specific options"""
    
    library_id = forms.IntegerField(
        min_value=1,
        max_value=999,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'readonly': True
        })
    )
    
    channel = forms.IntegerField(
        min_value=0,
        max_value=255,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'readonly': True
        })
    )
    
    target = forms.IntegerField(
        initial=0,
        min_value=0,
        max_value=255,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'readonly': True
        })
    )
    
    lun = forms.IntegerField(
        initial=0,
        min_value=0,
        max_value=255,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'readonly': True
        })
    )
    
    model_id = forms.ModelChoiceField(
        queryset=LibraryModel.objects.none(),
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Library Model'
    )
    
    product_revision = forms.CharField(
        initial='1068',
        max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'readonly': True
        })
    )
    
    unit_serial_number = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'readonly': True
        }),
        help_text='Auto-generated if not provided'
    )
    
    media_count = forms.IntegerField(
        initial=0,
        min_value=0,
        max_value=15000,
        widget=forms.NumberInput(attrs={
            'class': 'form-control'
        }),
        help_text='Number of media slots with cartridges'
    )
    
    empty_slots = forms.IntegerField(
        initial=0,
        min_value=0,
        max_value=15000,
        widget=forms.NumberInput(attrs={
            'class': 'form-control'
        }),
        help_text='Number of empty slots'
    )
    
    home_directory = forms.CharField(
        initial='/opt/mhvtl',
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control'
        })
    )

    def __init__(self, brand=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        if brand:
            self.fields['model_id'].queryset = LibraryModel.objects.filter(
                brand=brand, is_active=True
            ).order_by('name')

    def clean(self):
        cleaned_data = super().clean()
        media_count = cleaned_data.get('media_count', 0)
        empty_slots = cleaned_data.get('empty_slots', 0)
        
        # Validate total slots don't exceed 15000
        total_slots = media_count + empty_slots
        if total_slots > 15000:
            raise ValidationError(
                'Total number of library slots (media + empty) cannot exceed 15000. '
                f'Current total: {total_slots}'
            )
        
        return cleaned_data


class LibraryRemoveForm(forms.Form):
    """Form for removing a library"""
    
    library_id = forms.ModelChoiceField(
        queryset=Library.objects.none(),
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Select Library to Remove'
    )
    
    remove_media = forms.ChoiceField(
        choices=[('NO', 'No'), ('YES', 'Yes')],
        initial='NO',
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Remove ALL Tape Media Also?',
        help_text='WARNING: This will permanently remove all media from the library'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Only show active libraries, exclude if only one exists
        active_libraries = Library.objects.filter(is_active=True)
        if active_libraries.count() > 1:
            self.fields['library_id'].queryset = active_libraries.select_related('brand', 'model')
        else:
            self.fields['library_id'].queryset = Library.objects.none()

    def clean_library_id(self):
        library = self.cleaned_data['library_id']
        
        # Check if it's the last library
        if Library.objects.filter(is_active=True).count() <= 1:
            raise ValidationError('Cannot remove the last library configuration.')
        
        return library


class ResetDefaultForm(forms.Form):
    """Form for resetting all libraries to default"""
    
    remove_media = forms.ChoiceField(
        choices=[('NO', 'No'), ('YES', 'Yes')],
        initial='NO',
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Remove all tape media also?',
        help_text='WARNING: This will permanently remove all configured libraries, drives, and media!'
    )
    
    confirm = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        label='I understand this will remove ALL library configurations and cannot be undone'
    )


class DriveCreateForm(forms.ModelForm):
    """Form for creating drives"""
    
    class Meta:
        model = Drive
        fields = [
            'drive_id', 'channel', 'target', 'lun',
            'vendor_identification', 'product_identification',
            'product_revision', 'unit_serial_number'
        ]
        
        widgets = {
            'drive_id': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '1'
            }),
            'channel': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '255'
            }),
            'target': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '255'
            }),
            'lun': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'max': '255',
                'value': '0'
            }),
            'vendor_identification': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '50'
            }),
            'product_identification': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '100'
            }),
            'product_revision': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '20'
            }),
            'unit_serial_number': forms.TextInput(attrs={
                'class': 'form-control',
                'maxlength': '50'
            })
        }

    def __init__(self, library=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.library = library

    def clean_drive_id(self):
        drive_id = self.cleaned_data['drive_id']
        
        if self.library and Drive.objects.filter(
            library=self.library, 
            drive_id=drive_id, 
            is_active=True
        ).exists():
            raise ValidationError(f'Drive ID {drive_id} already exists in this library.')
        
        return drive_id