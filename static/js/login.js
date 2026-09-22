/**
 * Login Page JavaScript
 */

document.addEventListener('DOMContentLoaded', function() {
    const passwordField = document.getElementById('password');
    const loginForm = document.getElementById('loginForm');
    const loadingOverlay = document.getElementById('loadingOverlay');

    // Auto-focus on password field
    if (passwordField) {
        passwordField.focus();
    }

    // Add loading animation on form submit
    if (loginForm && loadingOverlay) {
        loginForm.addEventListener('submit', function() {
            loadingOverlay.style.display = 'flex';
        });
    }

    // Add keypress handler for Enter key
    if (passwordField) {
        passwordField.addEventListener('keypress', function(e) {
            if (e.key === 'Enter' && loginForm) {
                loginForm.submit();
            }
        });
    }

    // Add subtle parallax effect on mouse move
    const container = document.querySelector('.login-container');
    if (container) {
        document.addEventListener('mousemove', function(e) {
            const rect = container.getBoundingClientRect();
            const x = e.clientX - rect.left - rect.width / 2;
            const y = e.clientY - rect.top - rect.height / 2;

            const rotateX = (y / rect.height) * 5;
            const rotateY = (x / rect.width) * -5;

            container.style.transform = `perspective(1000px) rotateX(${rotateX}deg) rotateY(${rotateY}deg)`;
        });

        // Reset transform when mouse leaves
        document.addEventListener('mouseleave', function() {
            container.style.transform = 'perspective(1000px) rotateX(0deg) rotateY(0deg)';
        });
    }
});
