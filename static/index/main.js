// Composition root for the search/index page: initializes the search,
// suggestions, and library modules, and owns the one piece of genuinely
// cross-cutting behavior - the search box's keydown handling, which has to
// arbitrate between suggestion-list navigation and the plain-search Enter
// fallback.

import { runSearch, initSearch } from './search.js';
import {
  initSuggestions,
  isSuggestionsOpen,
  hasSuggestions,
  hasActiveSuggestion,
  moveActiveSuggestion,
  selectActiveSuggestion,
  closeSuggestions,
} from './suggestions.js';
import { initLibrary } from './library.js';

const queryInput = document.getElementById('query');

initSearch();
initSuggestions();
initLibrary();

queryInput.addEventListener('keydown', (e) => {
  if (isSuggestionsOpen() && hasSuggestions()) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveActiveSuggestion(1);
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveActiveSuggestion(-1);
      return;
    }
    if (e.key === 'Enter' && hasActiveSuggestion()) {
      e.preventDefault();
      selectActiveSuggestion();
      return;
    }
    if (e.key === 'Escape') {
      closeSuggestions();
      return;
    }
  }
  if (e.key === 'Enter') runSearch();
});
