// ===========================
// AI CALENDAR FUNCTIONALITY
// ===========================

let calendarState = {
    currentDate: new Date(),
    selectedDate: null,
    events: [],
    holidays: [],
    targets: { employees: [], teams: [] },
    settings: { country: 'IN', state: null },
    holidayWarningKey: null
};

// Initialize calendar on page load
document.addEventListener('DOMContentLoaded', initializeCalendar);

function initializeCalendar() {
    const toggle = document.getElementById('calendarToggle');
    const modal = document.getElementById('aiCalendarModal');
    const closeBtn = document.getElementById('aiCalendarClose');
    
    if (toggle) toggle.addEventListener('click', openCalendar);
    if (closeBtn) closeBtn.addEventListener('click', closeCalendar);
    
    setupCalendarEventListeners();
    setupCalendarSettings();
}

async function openCalendar() {
    document.getElementById('aiCalendarModal').classList.remove('hidden');
    document.getElementById('aiCalendarModal').classList.add('flex');
    const todayStr = new Date().toISOString().split('T')[0];
    const dateInput = document.getElementById('aiCalendarDate');
    if (dateInput) dateInput.setAttribute('min', todayStr);
    await loadCalendarSettings();
    renderCalendarMonth();
    loadCalendarEvents();
}

function closeCalendar() {
    document.getElementById('aiCalendarModal').classList.add('hidden');
    document.getElementById('aiCalendarModal').classList.remove('flex');
}

function setupCalendarEventListeners() {
    const prevBtn = document.getElementById('aiCalendarPrev');
    const nextBtn = document.getElementById('aiCalendarNext');
    const monthSelect = document.getElementById('aiCalendarMonth');
    const yearSelect = document.getElementById('aiCalendarYear');
    const dateInput = document.getElementById('aiCalendarDate');
    const addBtn = document.getElementById('aiCalendarAdd');
    const typeSelect = document.getElementById('aiCalendarType');
    
    if (prevBtn) prevBtn.addEventListener('click', previousMonth);
    if (nextBtn) nextBtn.addEventListener('click', nextMonth);
    if (monthSelect) monthSelect.addEventListener('change', changeMonth);
    if (yearSelect) yearSelect.addEventListener('change', changeYear);
    if (dateInput) dateInput.addEventListener('change', selectDate);
    if (addBtn) addBtn.addEventListener('click', addCalendarEvent);
    if (typeSelect) typeSelect.addEventListener('change', updateTargetSection);
}

function setupCalendarSettings() {
    const settingsToggle = document.getElementById('aiCalendarSettingsToggle');
    const settingsClose = document.getElementById('aiCalendarSettingsClose');
    const settingsModal = document.getElementById('aiCalendarSettings');
    const saveBtn = document.getElementById('aiCalendarSaveSettings');
    const countrySelect = document.getElementById('aiCalendarCountry');
    const stateSelect = document.getElementById('aiCalendarState');
    
    if (settingsToggle) settingsToggle.addEventListener('click', async () => {
        settingsModal.classList.toggle('hidden');
        settingsModal.classList.toggle('flex');
        if (!settingsModal.classList.contains('hidden')) {
            await loadCountries();
        }
    });
    if (settingsClose) settingsClose.addEventListener('click', () => {
        settingsModal.classList.add('hidden');
        settingsModal.classList.remove('flex');
    });
    if (saveBtn) saveBtn.addEventListener('click', saveCalendarSettings);
    if (countrySelect) countrySelect.addEventListener('change', loadStates);
}

async function loadCalendarSettings() {
    try {
        const res = await fetch('/api/calendar/settings');
        const data = await res.json();
        calendarState.settings = {
            country: data.country || 'IN',
            state: data.state || null
        };
    } catch (err) {
        console.error('Error loading calendar settings:', err);
    }
}

async function loadCalendarEvents(date = null) {
    try {
        const url = date ? `/api/calendar?date=${date}` : '/api/calendar';
        const res = await fetch(url);
        const data = await res.json();
        calendarState.events = data.events || [];
        
        // Load holidays for current month and year
        const year = calendarState.currentDate.getFullYear();
        const country = calendarState.settings.country || 'IN';
        const holidayRes = await fetch(`/api/calendar/holidays?year=${year}`);
        const holidayData = await holidayRes.json();
        calendarState.holidays = holidayData.holidays || [];

        const warningKey = `${year}|${country}|${calendarState.settings.state || ''}`;
        if (calendarState.holidayWarningKey !== warningKey) {
            if (holidayData.supported === false) {
                window.showToast?.('Holiday data not available for selected country.', 'warning');
                calendarState.holidayWarningKey = warningKey;
            } else if (holidayData.state_supported === false && calendarState.settings.state) {
                window.showToast?.('State holiday data not available for selected region.', 'warning');
                calendarState.holidayWarningKey = warningKey;
            }
        }
        
        renderCalendarMonth();
    } catch (err) {
        console.error('Error loading calendar events:', err);
    }
}

function renderCalendarMonth() {
    const year = calendarState.currentDate.getFullYear();
    const month = calendarState.currentDate.getMonth();
    
    // Update month/year selects
    const monthSelect = document.getElementById('aiCalendarMonth');
    const yearSelect = document.getElementById('aiCalendarYear');
    if (monthSelect) monthSelect.value = month;
    if (yearSelect) yearSelect.value = year;
    
    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const daysInPrevMonth = new Date(year, month, 0).getDate();
    
    let html = '<div class="border border-[var(--border)] p-4 rounded-lg"><div class="grid grid-cols-7 gap-1 text-center text-[10px] font-black uppercase tracking-widest mb-3">';
    
    // Day headers
    ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].forEach(day => {
        html += `<div class="text-slate-400 py-2">${day}</div>`;
    });
    
    html += '</div><div class="grid grid-cols-7 gap-1">';
    
    // Previous month days
    for (let i = firstDay - 1; i >= 0; i--) {
        html += `<button class="p-2 text-xs text-slate-300 bg-slate-50 rounded hover:bg-slate-100">${daysInPrevMonth - i}</button>`;
    }
    
    // Current month days
    for (let day = 1; day <= daysInMonth; day++) {
        const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const dayEvents = calendarState.events.filter(e => e.date === dateStr) || [];
        const dayHolidays = calendarState.holidays.filter(h => h.date === dateStr) || [];
        
        let bgClass = 'bg-white hover:bg-blue-50';
        let dotColors = [];
        
        // Collect holiday colors
        if (dayHolidays.length > 0) {
            dayHolidays.forEach(h => {
                if (h.type === 'national_holiday') dotColors.push('bg-red-500');
                else if (h.type === 'state_holiday') dotColors.push('bg-orange-500');
            });
        }
        
        // Collect event colors
        dayEvents.forEach(e => {
            if (e.type === 'personal_leave') dotColors.push('bg-amber-500');
            else if (e.type === 'office_holiday') dotColors.push('bg-purple-500');
            else if (e.type === 'meeting') dotColors.push('bg-blue-500');
            else if (e.type === 'task') dotColors.push('bg-emerald-500');
            else dotColors.push('bg-slate-500');
        });
        
        // Highlight today
        if (new Date(dateStr).toDateString() === new Date().toDateString()) {
            bgClass = 'bg-blue-100 border border-blue-400';
        }
        
        let dotHtml = '';
        if (dotColors.length > 0) {
            dotHtml = `<div class="flex flex-wrap gap-1 justify-center mt-1">${dotColors.slice(0, 4).map(c => `<span class="h-2.5 w-2.5 rounded-full ${c}" title="Event"></span>`).join('')}${dotColors.length > 4 ? `<span class="text-[8px] text-slate-400 font-bold">+${dotColors.length - 4}</span>` : ''}</div>`;
        }
        
        html += `<button class="p-2 text-xs ${bgClass} rounded cursor-pointer transition" onclick="selectDate('${dateStr}')">${day}${dotHtml}</button>`;
    }
    
    // Next month days
    for (let day = 1; day <= (42 - firstDay - daysInMonth); day++) {
        html += `<button class="p-2 text-xs text-slate-300 bg-slate-50 rounded hover:bg-slate-100">${day}</button>`;
    }
    
    html += '</div></div>';
    
    const grid = document.getElementById('aiCalendarGrid');
    if (grid) grid.innerHTML = html;
}

function selectDate(dateStr) {
    calendarState.selectedDate = dateStr;
    document.getElementById('aiCalendarDate').value = dateStr;
    
    const dayEvents = calendarState.events.filter(e => e.date === dateStr) || [];
    const dayHolidays = calendarState.holidays.filter(h => h.date === dateStr) || [];
    
    let listHtml = '';
    
    // Show national and state holidays
    dayHolidays.forEach(h => {
        const type = h.type === 'national_holiday' ? '🇮🇳 National Holiday' : '🏛️ State Holiday';
        listHtml += `<div class="text-[11px] text-slate-600 border-l-2 border-red-400 pl-2">${type}: ${h.title}</div>`;
    });
    
    // Show office holidays and personal events
    dayEvents.forEach(e => {
        const icons = {
            'meeting': '📞',
            'task': '📋',
            'personal_leave': '🏖️',
            'office_holiday': '🏢',
            'general': '📝'
        };
        const icon = icons[e.type] || '📌';
        const canDelete = e.type !== 'office_holiday' || user?.role === 'admin';
        listHtml += `<div class="text-[11px] text-slate-700 border-l-2 border-blue-400 pl-2">
            <div class="flex justify-between items-center"><span>${icon} ${e.title}</span>
            ${canDelete && e.id ? `<button onclick="deleteCalendarEvent(${e.id})" class="text-red-500 text-xs hover:text-red-700">×</button>` : ''}</div>`;
        // show attendees for meetings
        if (e.type === 'meeting' && e.attendees && Array.isArray(e.attendees) && e.attendees.length > 0) {
            listHtml += `<div class="text-[10px] text-slate-500 mt-1">Attendees: ${e.attendees.join(', ')}</div>`;
        }
        listHtml += `</div>`;
    });
    
    document.getElementById('aiCalendarList').innerHTML = listHtml || '<div class="text-slate-400 text-[11px]">No events</div>';
}

async function addCalendarEvent() {
    const date = document.getElementById('aiCalendarDate').value;
    const title = document.getElementById('aiCalendarTitle').value;
    const type = document.getElementById('aiCalendarType').value;
    const notes = document.getElementById('aiCalendarNotes').value;
    const todayStr = new Date().toISOString().split('T')[0];
    
    if (!date || !title) {
        window.showToast?.('Please fill in date and title', 'warning');
        return;
    }

    if (date < todayStr) {
        window.showToast?.('You cannot create events in the past.', 'warning');
        return;
    }
    
    const payload = { date, title, type, notes };
    
    // Add targets if admin and applicable
    if (type === 'office_holiday' && document.getElementById('aiCalendarTargetTeam')) {
        const teamId = document.getElementById('aiCalendarTargetTeam').value;
        if (teamId) payload.target_team_id = parseInt(teamId);
    }
    
    try {
        const res = await fetch('/api/calendar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (res.ok) {
            document.getElementById('aiCalendarTitle').value = '';
            document.getElementById('aiCalendarNotes').value = '';
            await loadCalendarEvents();
            selectDate(date);
        } else {
            const err = await res.json();
            window.showToast?.('Error: ' + (err.detail || 'Failed to add event'), 'error');
        }
    } catch (err) {
        console.error('Error adding calendar event:', err);
    }
}

async function deleteCalendarEvent(eventId) {
    const ok = window.showConfirm ? await window.showConfirm('Delete this event?') : confirm('Delete this event?');
    if (!ok) return;
    
    try {
        const res = await fetch(`/api/calendar/${eventId}`, { method: 'DELETE' });
        if (res.ok) {
            await loadCalendarEvents();
            selectDate(calendarState.selectedDate);
        }
    } catch (err) {
        console.error('Error deleting event:', err);
    }
}

function previousMonth() {
    const prevYear = calendarState.currentDate.getFullYear();
    calendarState.currentDate.setMonth(calendarState.currentDate.getMonth() - 1);
    const nextYear = calendarState.currentDate.getFullYear();
    if (prevYear !== nextYear) {
        loadCalendarEvents();
    } else {
        renderCalendarMonth();
    }
}

function nextMonth() {
    const prevYear = calendarState.currentDate.getFullYear();
    calendarState.currentDate.setMonth(calendarState.currentDate.getMonth() + 1);
    const nextYear = calendarState.currentDate.getFullYear();
    if (prevYear !== nextYear) {
        loadCalendarEvents();
    } else {
        renderCalendarMonth();
    }
}

function changeMonth(e) {
    calendarState.currentDate.setMonth(parseInt(e.target.value));
    renderCalendarMonth();
}

function changeYear(e) {
    calendarState.currentDate.setFullYear(parseInt(e.target.value));
    loadCalendarEvents();
}

async function loadCountries() {
    try {
        const countrySelect = document.getElementById('aiCalendarCountry');
        if (!countrySelect) return;
        
        // Show loading state
        countrySelect.innerHTML = '<option value="">Loading countries...</option>';
        
        const res = await fetch('/api/calendar/settings');
        const data = await res.json();
        const search = document.getElementById('aiCalendarCountrySearch');
        
        if (!data.countries || data.countries.length === 0) {
            countrySelect.innerHTML = '<option value="">No countries available</option>';
            return;
        }
        
        countrySelect.innerHTML = data.countries
            .map(c => {
                const isSelected = c.code && c.code === calendarState.settings.country;
                const selectedAttr = isSelected ? 'selected' : '';
                const dataCode = c.code || '';
                return `<option value="${c.name}" data-code="${dataCode}" ${selectedAttr}>${c.name}</option>`;
            })
            .join('');
        
        if (search) {
            search.value = '';
            search.addEventListener('input', (e) => {
                const query = e.target.value.toLowerCase();
                Array.from(countrySelect.options).forEach(opt => {
                    opt.hidden = !opt.text.toLowerCase().includes(query);
                });
            });
        }
        
        // Load states for selected country
        await loadStates();
    } catch (err) {
        console.error('Error loading countries:', err);
        const countrySelect = document.getElementById('aiCalendarCountry');
        if (countrySelect) {
            countrySelect.innerHTML = '<option value="">Error loading countries</option>';
        }
    }
}

async function loadStates() {
    const countrySelect = document.getElementById('aiCalendarCountry');
    const stateSelect = document.getElementById('aiCalendarState');
    if (!countrySelect || !stateSelect) return;
    
    const countryCode = countrySelect.options[countrySelect.selectedIndex]?.dataset?.code || '';
    
    try {
        // Show loading state
        stateSelect.innerHTML = '<option value="">Loading states...</option>';
        
        const res = await fetch(`/api/calendar/settings?country=${countryCode}`);
        const data = await res.json();
        const search = document.getElementById('aiCalendarStateSearch');
        
        const stateCodes = new Set((data.states || []).map(s => s.code));
        if (calendarState.settings.state && !stateCodes.has(calendarState.settings.state)) {
            calendarState.settings.state = null;
        }

        stateSelect.innerHTML = '<option value="">None (National Holidays Only)</option>' +
            (data.states || []).map(s => `<option value="${s.code}" ${s.code === calendarState.settings.state ? 'selected' : ''}>${s.name}</option>`)
            .join('');
        
        if (search) {
            search.value = '';
            search.addEventListener('input', (e) => {
                const query = e.target.value.toLowerCase();
                Array.from(stateSelect.options).forEach(opt => {
                    opt.hidden = !opt.text.toLowerCase().includes(query);
                });
            });
        }
    } catch (err) {
        console.error('Error loading states:', err);
        stateSelect.innerHTML = '<option value="">Error loading states</option>';
    }
}

async function saveCalendarSettings() {
    const countryOption = document.getElementById('aiCalendarCountry').options[
        document.getElementById('aiCalendarCountry').selectedIndex
    ];
    const country = countryOption?.dataset?.code || countryOption?.value || '';
    const state = document.getElementById('aiCalendarState').value;
    
    try {
        const res = await fetch('/api/calendar/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ country, state })
        });
        
        if (res.ok) {
            calendarState.settings = { country, state };
            document.getElementById('aiCalendarSettings').classList.add('hidden');
            document.getElementById('aiCalendarSettings').classList.remove('flex');
            // Reload calendar to show holidays for new country/state
            await loadCalendarEvents();
            const countryName = document.querySelector('#aiCalendarCountry option:checked')?.text || country;
            const stateName = document.querySelector('#aiCalendarState option:checked')?.text || state;
            window.showToast?.('Calendar settings updated! Now showing holidays for ' + countryName + (state ? ' - ' + stateName : ''), 'success');
        }
    } catch (err) {
        console.error('Error saving calendar settings:', err);
    }
}

function updateTargetSection() {
    const type = document.getElementById('aiCalendarType').value;
    const section = document.getElementById('aiCalendarTargetSection');
    if (section) {
        section.classList.toggle('hidden', type !== 'office_holiday');
    }
}

// Simple JS for interactivity (e.g., listing block persons)
async function listBlock(block) {
    const response = await fetch(`/api/block_persons?block=${block}`);
    const data = await response.json();
    const listDiv = document.getElementById('block-list');
    listDiv.innerHTML = `<h3>Persons in ${block}</h3><ul>${data.persons.map(p => `<li>${p.name}</li>`).join('')}</ul>`;
}