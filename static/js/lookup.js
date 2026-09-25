let denchiScanController = null
let denchiLookupController = null
let denchiLookupGeneration = 0
const denchiAutoPrintTimes = new Map()

window.app = Vue.createApp({
  el: '#vue',
  delimiters: ['[[', ']]'],
  data() {
    return {
      scanning: false,
      starting: false,
      loading: false,
      error: null,
      result: null,
      currentLnurlwUri: null,
      printing: false
    }
  },
  methods: {
    async startScan(automatic = false) {
      if (this.scanning || this.starting) {
        return
      }
      if (!window.isSecureContext) {
        if (!automatic) {
          this.error = 'Web NFC requires a secure HTTPS connection.'
        }
        return
      }
      if (!('NDEFReader' in window)) {
        if (!automatic) {
          this.error =
            'Web NFC is not supported by this browser. Use Android Chrome.'
        }
        return
      }

      this.starting = true
      this.error = null
      const controller = new AbortController()
      denchiScanController = controller
      const reader = new NDEFReader()
      reader.addEventListener('reading', event => this.handleReading(event))
      reader.addEventListener('readingerror', () => {
        this.error = 'This NFC tag could not be read. Tap another card.'
      })

      try {
        await reader.scan({signal: controller.signal})
        if (denchiScanController === controller) {
          this.scanning = true
        }
      } catch (error) {
        if (!automatic && error.name !== 'AbortError') {
          this.error = error.message || 'Could not start NFC scanning.'
        }
      } finally {
        this.starting = false
        if (!this.scanning && denchiScanController === controller) {
          denchiScanController = null
        }
      }
    },
    stopScan() {
      if (denchiScanController) {
        denchiScanController.abort()
        denchiScanController = null
      }
      if (denchiLookupController) {
        denchiLookupController.abort()
        denchiLookupController = null
      }
      denchiLookupGeneration += 1
      this.scanning = false
      this.loading = false
    },
    ndefUrl(message) {
      for (const record of message.records) {
        if (record.recordType === 'url' && record.data) {
          return new TextDecoder().decode(record.data)
        }
      }
      return null
    },
    async handleReading(event) {
      const generation = ++denchiLookupGeneration
      if (denchiLookupController) {
        denchiLookupController.abort()
        denchiLookupController = null
      }
      const lnurlwUri = this.ndefUrl(event.message)
      if (!lnurlwUri || !lnurlwUri.toLowerCase().startsWith('lnurlw://')) {
        this.loading = false
        this.result = null
        this.currentLnurlwUri = null
        this.error = 'This is not a Card NFC tag.'
        return
      }

      const controller = new AbortController()
      denchiLookupController = controller
      this.loading = true
      this.error = null

      try {
        const response = await fetch('/denchi/api/v1/public/lookup', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({lnurlw_uri: lnurlwUri}),
          signal: controller.signal
        })
        if (!response.ok) {
          throw new Error('Card could not be found.')
        }
        const result = await response.json()
        if (generation === denchiLookupGeneration) {
          this.result = result
          this.currentLnurlwUri = lnurlwUri
          if (window.denchiReceiptTerminal?.autoPrint) {
            this.autoPrintReceipt(lnurlwUri)
          }
        }
      } catch (error) {
        if (
          error.name !== 'AbortError' &&
          generation === denchiLookupGeneration
        ) {
          this.result = null
          this.currentLnurlwUri = null
          this.error = error.message || 'Card could not be found.'
        }
      } finally {
        if (generation === denchiLookupGeneration) {
          this.loading = false
          denchiLookupController = null
        }
      }
    },
    autoPrintReceipt(lnurlwUri) {
      const now = Date.now()
      if (now - (denchiAutoPrintTimes.get(lnurlwUri) ?? -Infinity) < 5000) {
        return
      }
      denchiAutoPrintTimes.set(lnurlwUri, now)
      for (const [uri, printedAt] of denchiAutoPrintTimes) {
        if (now - printedAt >= 5000) {
          denchiAutoPrintTimes.delete(uri)
        }
      }
      this.printReceipt('auto', lnurlwUri)
    },
    async printReceipt(mode, lnurlwUri = this.currentLnurlwUri) {
      const terminal = window.denchiReceiptTerminal
      if (!terminal || !lnurlwUri || (mode === 'manual' && this.printing)) {
        return
      }
      if (mode === 'manual') {
        this.printing = true
      }
      try {
        const response = await fetch('/denchi/api/v1/public/print-receipt', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            lnurlw_uri: lnurlwUri,
            printer_token: terminal.token,
            mode
          })
        })
        if (!response.ok) {
          throw new Error('Receipt printer is unavailable.')
        }
        const result = await response.json()
        if (result.printed) {
          this.$q.notify({
            type: 'positive',
            message: 'Receipt sent to printer.'
          })
        }
      } catch (error) {
        this.$q.notify({
          type: mode === 'auto' ? 'warning' : 'negative',
          message: error.message || 'Receipt printer is unavailable.'
        })
      } finally {
        if (mode === 'manual') {
          this.printing = false
        }
      }
    },
    formatSats(value) {
      return Number(value).toLocaleString()
    },
    formatTimestamp(value) {
      return new Date(value).toLocaleString()
    }
  },
  mounted() {
    this.startScan(true)
  },
  beforeUnmount() {
    this.stopScan()
  }
})
