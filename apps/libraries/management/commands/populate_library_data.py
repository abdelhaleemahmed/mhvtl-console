# apps/libraries/management/commands/populate_library_data.py
from django.core.management.base import BaseCommand
from apps.libraries.models import LibraryBrand, LibraryModel


class Command(BaseCommand):
    help = 'Populate initial library brands and models from original MHVTL system'

    def handle(self, *args, **options):
        self.stdout.write('Populating library brands and models...')

        # Create library brands based on original PHP system
        brands_data = [
            {'name': 'STK', 'display_name': 'STK', 'color_code': '#FF0000'},
            {'name': 'IBM', 'display_name': 'IBM', 'color_code': '#000000'},
            {'name': 'HP', 'display_name': 'HP', 'color_code': '#0000FF'},
            {'name': 'Spectra', 'display_name': 'Spectra Logic', 'color_code': '#000000'},
            {'name': 'Quantum', 'display_name': 'Quantum', 'color_code': '#000000'},
            {'name': 'ADIC', 'display_name': 'ADIC', 'color_code': '#000000'},
            {'name': 'Sony', 'display_name': 'Sony', 'color_code': '#000000'},
            {'name': 'Dell', 'display_name': 'Dell', 'color_code': '#000000'},
            {'name': 'Overland', 'display_name': 'Overland Storage', 'color_code': '#000000'},
        ]

        brands = {}
        for brand_data in brands_data:
            brand, created = LibraryBrand.objects.get_or_create(
                name=brand_data['name'],
                defaults={
                    'display_name': brand_data['display_name'],
                    'color_code': brand_data['color_code'],
                    'is_active': True
                }
            )
            brands[brand.name] = brand
            if created:
                self.stdout.write(f'Created brand: {brand.display_name}')
            else:
                self.stdout.write(f'Brand already exists: {brand.display_name}')

        # Create library models for each brand
        # NOTE: These models are validated against MHVTL PHP GUI (mhvtl-gui-master)
        # Only include models that MHVTL actually supports to avoid VPD allocation crashes
        models_data = {
            'STK': [
                # Library models - validated from PHP GUI form.add.stk.library.php
                {'name': 'SL150', 'product_id': 'SL150'},
                {'name': 'SL500', 'product_id': 'SL500'},
                {'name': 'SL3000', 'product_id': 'SL3000'},
                {'name': 'L700', 'product_id': 'L700'},
                {'name': 'L180', 'product_id': 'L180'},
                {'name': 'L120', 'product_id': 'L120'},
                {'name': 'L80', 'product_id': 'L80'},
            ],
            'IBM': [
                # Library models - validated from PHP GUI form.add.ibm.library.php
                {'name': '03584L22', 'product_id': '03584L22'},
                {'name': '03584L32', 'product_id': '03584L32'},
                {'name': 'ULT3582-TL', 'product_id': 'ULT3582-TL'},
                {'name': '3577-TL', 'product_id': '3577-TL'},
                {'name': '3573-TL', 'product_id': '3573-TL'},
                {'name': '03590H11', 'product_id': '03590H11'},
            ],
            'HP': [
                # Library models - validated from PHP GUI form.add.hp.library.php
                {'name': 'EML E-Series', 'product_id': 'EML E-Series'},
                {'name': 'MSL G3 Series', 'product_id': 'MSL G3 Series'},
                {'name': 'MSL6000 Series', 'product_id': 'MSL6000 Series'},
                {'name': 'ESL E-Series', 'product_id': 'ESL E-Series'},
                {'name': 'ThinStor AutoLdr', 'product_id': 'ThinStor AutoLdr'},
                {'name': '1x8 autoloader', 'product_id': '1x8 autoloader'},
                {'name': 'VLS', 'product_id': 'VLS'},
            ],
            'Spectra': [
                # Library models - validated from PHP GUI form.add.spectra.library.php
                # Vendor name is "SPECTRA" in device.conf
                {'name': 'PYTHON', 'product_id': 'PYTHON'},
                {'name': 'GECKO', 'product_id': 'GECKO'},
            ],
            'Quantum': [
                # Library models - validated from PHP GUI form.add.quantum.library.php
                {'name': 'Scalar 24', 'product_id': 'Scalar 24'},
                {'name': 'Scalar 100', 'product_id': 'Scalar 100'},
                {'name': 'Scalar 1000', 'product_id': 'Scalar 1000'},
                {'name': 'Scalar i40', 'product_id': 'Scalar i40'},
                {'name': 'Scalar i80', 'product_id': 'Scalar i80'},
                {'name': 'Scalar i500', 'product_id': 'Scalar i500'},
                {'name': 'Scalar i2000', 'product_id': 'Scalar i2000'},
                {'name': 'Scalar i6000', 'product_id': 'Scalar i6000'},
                {'name': 'SuperLoader 3', 'product_id': 'SuperLoader 3'},
            ],
            'ADIC': [
                # Library models - validated from PHP GUI form.add.adic.library.php
                # IMPORTANT: "Scalar 10000" is NOT a valid MHVTL product - causes VPD crash
                {'name': 'Scalar i2000', 'product_id': 'Scalar i2000'},
                {'name': 'Scalar 1000', 'product_id': 'Scalar 1000'},
            ],
            'Sony': [
                # Library models - validated from PHP GUI form.add.sony.library.php
                {'name': 'TSL-A500C', 'product_id': 'TSL-A500C'},
                {'name': 'LIB-81', 'product_id': 'LIB-81'},
                {'name': 'LIB-162', 'product_id': 'LIB-162'},
                {'name': 'LIB-304', 'product_id': 'LIB-304'},
            ],
            'Dell': [
                # Library models - validated from PHP GUI form.add.dell.library.php
                {'name': 'PowerVault TL2000', 'product_id': 'PV-TL2000'},
                {'name': 'PowerVault TL4000', 'product_id': 'PV-TL4000'},
                {'name': 'PowerVault ML6000', 'product_id': 'PV-ML6000'},
            ],
            'Overland': [
                # Library models - validated from PHP GUI form.add.overland.library.php
                {'name': 'Neo 200s', 'product_id': 'NEO 200s'},
                {'name': 'Neo 400s', 'product_id': 'NEO 400s'},
                {'name': 'Neo 2000', 'product_id': 'NEO 2000'},
                {'name': 'Neo 4000', 'product_id': 'NEO 4000'},
                {'name': 'Neo 8000', 'product_id': 'NEO 8000'},
            ],
        }

        total_models = 0
        for brand_name, models in models_data.items():
            if brand_name in brands:
                brand = brands[brand_name]
                for model_data in models:
                    model, created = LibraryModel.objects.get_or_create(
                        brand=brand,
                        name=model_data['name'],
                        defaults={
                            'product_identification': model_data['product_id'],
                            'default_revision': '1068',
                            'is_active': True
                        }
                    )
                    total_models += 1
                    if created:
                        self.stdout.write(f'  Created model: {brand.name} - {model.name}')

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully populated {len(brands)} brands and {total_models} models!'
            )
        )

        # Display summary
        self.stdout.write('\nSummary:')
        for brand_name, brand in brands.items():
            model_count = brand.models.filter(is_active=True).count()
            self.stdout.write(f'  {brand.display_name}: {model_count} models')
        
        self.stdout.write(
            self.style.SUCCESS('\nLibrary data population completed successfully!')
        )