/**
 * MHVTL Console - Base JavaScript
 * Common functionality used across all pages
 */

(function() {
    'use strict';

    // CSRF Token Helper for AJAX requests
    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    // Make CSRF token available globally
    window.csrfToken = getCookie('csrftoken');

    // Auto-dismiss alerts after 5 seconds
    function initAlerts() {
        const alerts = document.querySelectorAll('.alert');
        alerts.forEach(function(alert) {
            setTimeout(function() {
                alert.style.opacity = '0';
                alert.style.transform = 'translateY(-20px)';
                setTimeout(function() {
                    alert.remove();
                }, 300);
            }, 5000);
        });
    }

    // Form validation helper
    function validateForm(formElement) {
        const inputs = formElement.querySelectorAll('input[required], select[required], textarea[required]');
        let isValid = true;

        inputs.forEach(function(input) {
            if (!input.value.trim()) {
                input.classList.add('is-invalid');
                isValid = false;
            } else {
                input.classList.remove('is-invalid');
            }
        });

        return isValid;
    }

    // Loading overlay helper
    function showLoading(message = 'Loading...') {
        let overlay = document.getElementById('loading-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'loading-overlay';
            overlay.className = 'loading-overlay';
            overlay.innerHTML = `
                <div class="loading-content">
                    <div class="spinner"></div>
                    <p class="loading-message">${message}</p>
                </div>
            `;
            document.body.appendChild(overlay);
        }
        overlay.style.display = 'flex';
    }

    function hideLoading() {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) {
            overlay.style.display = 'none';
        }
    }

    // Confirmation dialog helper
    function confirmAction(message, callback) {
        if (confirm(message)) {
            callback();
        }
    }

    // Format date helper
    function formatDate(dateString) {
        const date = new Date(dateString);
        return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
    }

    // Format SCSI address helper
    function formatSCSIAddress(channel, target, lun) {
        return `${channel}:${target}:${lun}`;
    }

    // Initialize on page load
    document.addEventListener('DOMContentLoaded', function() {
        // Initialize alerts
        initAlerts();

        // CSRF headers are handled in static/js/csrf.js, which is loaded
        // first and covers fetch, XMLHttpRequest and htmx. The patch that used
        // to live here sent the token to cross-origin URLs too.

        // Add smooth scroll to anchor links
        document.querySelectorAll('a[href^="#"]').forEach(anchor => {
            anchor.addEventListener('click', function (e) {
                const target = document.querySelector(this.getAttribute('href'));
                if (target) {
                    e.preventDefault();
                    target.scrollIntoView({
                        behavior: 'smooth'
                    });
                }
            });
        });

        // Add confirmation to delete buttons
        document.querySelectorAll('[data-confirm]').forEach(function(element) {
            element.addEventListener('click', function(e) {
                const message = this.getAttribute('data-confirm') || 'Are you sure?';
                if (!confirm(message)) {
                    e.preventDefault();
                    return false;
                }
            });
        });

        // Auto-focus on first input in forms
        const firstInput = document.querySelector('form input:not([type="hidden"]):not([readonly])');
        if (firstInput) {
            firstInput.focus();
        }
    });

    // Export utilities to window for global access
    window.MHVTLUtils = {
        getCookie,
        validateForm,
        showLoading,
        hideLoading,
        confirmAction,
        formatDate,
        formatSCSIAddress
    };

})();
