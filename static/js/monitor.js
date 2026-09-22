/**
 * Library Monitor Page JavaScript
 */

(function() {
    'use strict';

    let refreshInterval = null;
    const REFRESH_RATE = 30000; // 30 seconds

    // Refresh dashboard data via AJAX
    function refreshData() {
        const libraryId = getLibraryIdFromURL();
        if (!libraryId) {
            console.error('Library ID not found in URL');
            return;
        }

        // Show loading indicator
        showRefreshIndicator();

        // Fetch updated status
        fetch(`/libraries/api/status/${libraryId}/`, {
            method: 'GET',
            headers: {
                'X-CSRFToken': window.csrfToken || '',
                'Content-Type': 'application/json'
            }
        })
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.json();
        })
        .then(data => {
            updateStatusCards(data);
            updateSystemMetrics(data.metrics);
            updateDriveStatus(data.drives);
            hideRefreshIndicator();
        })
        .catch(error => {
            console.error('Error refreshing data:', error);
            hideRefreshIndicator();
        });
    }

    // Extract library ID from URL
    function getLibraryIdFromURL() {
        const pathParts = window.location.pathname.split('/');
        // Look for 'monitor' in the path (e.g., /libraries/monitor/20/)
        const monitorIndex = pathParts.indexOf('monitor');
        if (monitorIndex !== -1 && pathParts[monitorIndex + 1]) {
            return pathParts[monitorIndex + 1];
        }
        // Fallback: look for 'library' in path
        const libraryIndex = pathParts.indexOf('library');
        if (libraryIndex !== -1 && pathParts[libraryIndex + 1]) {
            return pathParts[libraryIndex + 1];
        }
        return null;
    }

    // Update status cards
    function updateStatusCards(data) {
        // Update drives online count
        const drivesOnline = document.querySelector('.status-card:nth-child(2) .status-value');
        if (drivesOnline && data.drive_status) {
            drivesOnline.textContent = `${data.drive_status.online}/${data.drive_status.total}`;
        }

        // Update error count
        const errorCount = document.querySelector('.status-card:nth-child(4) .status-value');
        const errorIcon = document.querySelector('.status-card:nth-child(4) .status-icon');
        if (errorCount && data.error_count !== undefined) {
            errorCount.textContent = data.error_count;
            if (data.error_count > 0) {
                errorCount.className = 'status-value status-error';
                if (errorIcon) errorIcon.textContent = '⚠️';
            } else {
                errorCount.className = 'status-value status-online';
                if (errorIcon) errorIcon.textContent = '✅';
            }
        }
    }

    // Update system metrics
    function updateSystemMetrics(metrics) {
        if (!metrics) return;

        const cpuElement = document.getElementById('cpu-usage');
        const memoryElement = document.getElementById('memory-usage');
        const diskElement = document.getElementById('disk-usage');
        const uptimeElement = document.getElementById('uptime');

        if (cpuElement && metrics.cpu != null) {
            cpuElement.textContent = metrics.cpu + '%';
        }
        if (memoryElement && metrics.memory != null) {
            memoryElement.textContent = metrics.memory + '%';
        }
        if (diskElement && metrics.disk != null) {
            diskElement.textContent = metrics.disk + '%';
        }
        if (uptimeElement && metrics.uptime) {
            uptimeElement.textContent = metrics.uptime;
        }
    }

    // Update drive status
    function updateDriveStatus(drives) {
        if (!drives || !Array.isArray(drives)) return;

        drives.forEach(drive => {
            const driveCard = document.querySelector(`[data-drive-id="${drive.id}"]`);
            if (driveCard) {
                const statusBadge = driveCard.querySelector('.drive-status');
                if (statusBadge) {
                    statusBadge.textContent = drive.status;
                    statusBadge.className = `drive-status ${drive.status.toLowerCase()}`;
                }
            }
        });
    }

    // Show refresh indicator
    function showRefreshIndicator() {
        const indicators = document.querySelectorAll('.pulse');
        indicators.forEach(indicator => {
            indicator.style.animation = 'pulse 0.5s infinite';
        });
    }

    // Hide refresh indicator
    function hideRefreshIndicator() {
        const indicators = document.querySelectorAll('.pulse');
        indicators.forEach(indicator => {
            indicator.style.animation = 'pulse 2s infinite';
        });
    }

    // Start auto-refresh
    function startAutoRefresh() {
        if (refreshInterval) {
            clearInterval(refreshInterval);
        }
        refreshInterval = setInterval(refreshData, REFRESH_RATE);
    }

    // Stop auto-refresh
    function stopAutoRefresh() {
        if (refreshInterval) {
            clearInterval(refreshInterval);
            refreshInterval = null;
        }
    }

    // Initialize on page load
    document.addEventListener('DOMContentLoaded', function() {
        // Fetch data immediately on page load
        refreshData();

        // Start auto-refresh (every 30 seconds)
        startAutoRefresh();

        // Manual refresh button
        const refreshBtn = document.querySelector('.header-btn.refresh');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', function(e) {
                e.preventDefault();
                refreshData();
            });
        }

        // Stop refresh when page is hidden
        document.addEventListener('visibilitychange', function() {
            if (document.hidden) {
                stopAutoRefresh();
            } else {
                startAutoRefresh();
            }
        });

        // Stop refresh on page unload
        window.addEventListener('beforeunload', function() {
            stopAutoRefresh();
        });
    });

    // Export functions to window for external access
    window.MonitorPage = {
        refreshData,
        startAutoRefresh,
        stopAutoRefresh
    };

})();
