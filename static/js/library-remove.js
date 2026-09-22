/**
 * Library Remove Page JavaScript
 * Handles library selection, preview, and confirmation
 */

document.addEventListener('DOMContentLoaded', function() {
    const librarySelect = document.getElementById('librarySelect');
    const libraryPreview = document.getElementById('libraryPreview');
    const previewIcon = document.getElementById('previewIcon');
    const previewTitle = document.getElementById('previewTitle');
    const previewMeta = document.getElementById('previewMeta');
    const confirmCheckbox = document.getElementById('confirmRemoval');
    const confirmationSection = document.getElementById('confirmationSection');
    const confirmationText = document.getElementById('confirmationText');
    const mediaAction = document.getElementById('mediaAction');
    const removeBtn = document.getElementById('removeBtn');
    const removeForm = document.getElementById('removeForm');
    const removeText = document.getElementById('removeText');
    const removeSpinner = document.getElementById('removeSpinner');

    // Return early if elements don't exist (empty state)
    if (!librarySelect) return;

    // Update preview when library changes
    librarySelect.addEventListener('change', function() {
        const selectedOption = this.options[this.selectedIndex];

        if (selectedOption.value) {
            const brand = selectedOption.dataset.brand;
            const model = selectedOption.dataset.model;
            const serial = selectedOption.dataset.serial || 'N/A';
            const status = selectedOption.dataset.status || 'unknown';

            previewTitle.textContent = `Library ${selectedOption.value}: ${brand} ${model}`;
            previewMeta.textContent = `Serial: ${serial} • Status: ${status}`;
            libraryPreview.style.display = 'block';

            updateConfirmation();
        } else {
            libraryPreview.style.display = 'none';
            confirmationSection.style.display = 'none';
        }

        updateRemoveButton();
    });

    // Update confirmation details
    function updateConfirmation() {
        if (librarySelect.value && confirmCheckbox.checked) {
            const selectedOption = librarySelect.options[librarySelect.selectedIndex];
            const brand = selectedOption.dataset.brand;
            const model = selectedOption.dataset.model;

            confirmationText.textContent = `Library ${librarySelect.value}: ${brand} ${model}`;
            confirmationSection.style.display = 'block';
        } else {
            confirmationSection.style.display = 'none';
        }
    }

    // Update media action text
    const mediaSelect = document.querySelector('select[name="remove_media"]');
    if (mediaSelect) {
        mediaSelect.addEventListener('change', function() {
            if (this.value === 'YES') {
                mediaAction.textContent = 'Remove ALL tape media permanently';
                mediaAction.style.color = '#e74c3c';
                mediaAction.style.fontWeight = 'bold';
            } else {
                mediaAction.textContent = 'Keep all tape media (media will remain)';
                mediaAction.style.color = '#27ae60';
                mediaAction.style.fontWeight = 'normal';
            }
        });
    }

    // Update remove button state
    function updateRemoveButton() {
        const isValid = librarySelect.value && confirmCheckbox.checked;
        removeBtn.disabled = !isValid;
    }

    // Handle confirmation checkbox
    confirmCheckbox.addEventListener('change', function() {
        updateConfirmation();
        updateRemoveButton();
    });

    // Handle form submission
    if (removeForm) {
        removeForm.addEventListener('submit', function(e) {
            if (!librarySelect.value || !confirmCheckbox.checked) {
                e.preventDefault();
                alert('Please select a library and confirm the removal.');
                return;
            }

            // Show loading state
            removeBtn.disabled = true;
            removeText.style.display = 'none';
            removeSpinner.style.display = 'inline-block';

            // Final confirmation
            const selectedOption = librarySelect.options[librarySelect.selectedIndex];
            const brand = selectedOption.dataset.brand;
            const model = selectedOption.dataset.model;

            if (!confirm(`Are you absolutely sure you want to remove Library ${librarySelect.value}: ${brand} ${model}?\n\nThis action cannot be undone!`)) {
                e.preventDefault();
                // Reset loading state
                removeBtn.disabled = false;
                removeText.style.display = 'inline';
                removeSpinner.style.display = 'none';
            }
        });
    }

    // Keyboard shortcuts
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            const dashboardUrl = removeForm ? removeForm.dataset.dashboardUrl : null;
            if (dashboardUrl) {
                window.location.href = dashboardUrl;
            }
        }
    });
});
