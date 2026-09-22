/**
 * Reset Default Page JavaScript
 * Handles multiple confirmations for dangerous reset operation
 */

document.addEventListener('DOMContentLoaded', function() {
    const mediaSelect = document.getElementById('mediaSelect');
    const mediaActionText = document.getElementById('mediaActionText');
    const finalConfirmation = document.getElementById('finalConfirmation');
    const resetBtn = document.getElementById('resetBtn');
    const resetForm = document.getElementById('resetForm');
    const resetText = document.getElementById('resetText');
    const resetSpinner = document.getElementById('resetSpinner');

    const checkboxes = [
        document.getElementById('confirm1'),
        document.getElementById('confirm2'),
        document.getElementById('confirm3'),
        document.getElementById('confirm4'),
        document.getElementById('confirmFinal')
    ];

    // Update media action text
    if (mediaSelect) {
        mediaSelect.addEventListener('change', function() {
            if (this.value === 'YES') {
                mediaActionText.textContent = 'Remove ALL tape media permanently';
                mediaActionText.style.color = '#e74c3c';
                mediaActionText.style.fontWeight = 'bold';
            } else if (this.value === 'NO') {
                mediaActionText.textContent = 'Keep all tape media (recommended)';
                mediaActionText.style.color = '#27ae60';
                mediaActionText.style.fontWeight = 'normal';
            } else {
                mediaActionText.textContent = 'Please select media option';
                mediaActionText.style.color = '#6c757d';
                mediaActionText.style.fontWeight = 'normal';
            }
            updateResetButton();
        });
    }

    // Update reset button state
    function updateResetButton() {
        const allChecked = checkboxes.every(cb => cb && cb.checked);
        const mediaSelected = mediaSelect && mediaSelect.value !== '';
        const isValid = allChecked && mediaSelected;

        if (resetBtn) {
            resetBtn.disabled = !isValid;
        }

        if (finalConfirmation) {
            if (isValid) {
                finalConfirmation.style.display = 'block';
            } else {
                finalConfirmation.style.display = 'none';
            }
        }
    }

    // Add event listeners to all checkboxes
    checkboxes.forEach(checkbox => {
        if (checkbox) {
            checkbox.addEventListener('change', updateResetButton);
        }
    });

    // Handle form submission
    if (resetForm) {
        resetForm.addEventListener('submit', function(e) {
            const allChecked = checkboxes.every(cb => cb && cb.checked);
            const mediaSelected = mediaSelect && mediaSelect.value !== '';

            if (!allChecked || !mediaSelected) {
                e.preventDefault();
                alert('Please complete all confirmations and select media handling option.');
                return;
            }

            // Triple confirmation for this dangerous action
            const mediaAction = mediaSelect.value === 'YES' ? 'AND REMOVE ALL MEDIA' : 'but keep media';

            if (!confirm(`⚠️ FINAL WARNING ⚠️\n\nYou are about to PERMANENTLY RESET ALL MHVTL SETTINGS ${mediaAction.toUpperCase()}!\n\nThis action CANNOT be undone!\n\nClick OK only if you are absolutely certain.`)) {
                e.preventDefault();
                return;
            }

            if (!confirm(`Last chance to cancel!\n\nType "RESET" and click OK to confirm, or Cancel to abort.`)) {
                e.preventDefault();
                return;
            }

            // Show loading state
            if (resetBtn && resetText && resetSpinner) {
                resetBtn.disabled = true;
                resetText.style.display = 'none';
                resetSpinner.style.display = 'inline-block';
            }

            // Add a small delay to show the loading state
            setTimeout(() => {
                // Form will submit normally
            }, 1000);
        });
    }

    // Keyboard shortcuts
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            if (confirm('Are you sure you want to cancel the reset process?')) {
                const dashboardUrl = resetForm ? resetForm.dataset.dashboardUrl : null;
                if (dashboardUrl) {
                    window.location.href = dashboardUrl;
                }
            }
        }
    });

    // Add extra protection - prevent accidental refresh
    window.addEventListener('beforeunload', function(e) {
        if (checkboxes.some(cb => cb && cb.checked)) {
            e.preventDefault();
            e.returnValue = 'You have started the reset process. Are you sure you want to leave?';
        }
    });
});
