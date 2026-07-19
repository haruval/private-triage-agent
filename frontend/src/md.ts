// Material Web component registry. Each component module is imported exactly
// once, here, and rendered everywhere else as plain custom elements
// (<md-filled-button> etc. — React 19 supports custom-element properties and
// events natively). Everything is bundled locally: no CDNs, no external
// fonts — email content must never cause a third-party fetch.
import '@material/web/button/filled-button.js'
import '@material/web/button/filled-tonal-button.js'
import '@material/web/button/outlined-button.js'
import '@material/web/button/text-button.js'
import '@material/web/divider/divider.js'
import '@material/web/list/list.js'
import '@material/web/list/list-item.js'
import '@material/web/progress/circular-progress.js'
import '@material/web/progress/linear-progress.js'
import '@material/web/select/outlined-select.js'
import '@material/web/select/select-option.js'
import '@material/web/textfield/outlined-text-field.js'

import { styles as typescaleStyles } from '@material/web/typography/md-typescale-styles.js'

if (typescaleStyles.styleSheet) {
  document.adoptedStyleSheets.push(typescaleStyles.styleSheet)
}
