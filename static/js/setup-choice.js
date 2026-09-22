/**
 * Setup Choice Page JavaScript
 * Handles option selection and form validation
 */

function selectOption(optionType) {
    // Remove selected class from all cards
    document.querySelectorAll('.option-card').forEach(card => {
        card.classList.remove('selected');
    });

    // Add selected class to clicked card
    event.currentTarget.classList.add('selected');

    // Check the radio button
    document.getElementById(optionType).checked = true;

    // Enable continue button
    document.getElementById('continueBtn').disabled = false;
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    // Handle form submission
    const setupForm = document.querySelector('.setup-form');
    if (setupForm) {
        setupForm.addEventListener('submit', function(e) {
            const selectedOption = document.querySelector('input[name="setup_type"]:checked');
            if (!selectedOption) {
                e.preventDefault();
                alert('Please select a configuration type');
            }
        });
    }

    // Add keyboard navigation
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' || e.key === ' ') {
            const focusedElement = document.activeElement;
            if (focusedElement.classList.contains('option-card')) {
                e.preventDefault();
                focusedElement.click();
            }
        }
    });

    // Check if any option is pre-selected
    const preSelected = document.querySelector('input[name="setup_type"]:checked');
    if (preSelected) {
        const card = document.querySelector(`label[for="${preSelected.id}"]`);
        if (card) {
            card.classList.add('selected');
            document.getElementById('continueBtn').disabled = false;
        }
    }
});
