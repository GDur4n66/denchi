window.app = Vue.createApp({
  el: '#vue',
  delimiters: ['[[', ']]'],
  mixins: [windowMixin],
  data() {
    return {
      loading: false,
      profiles: [],
      issueProfileId: null,
      issuing: false,
      cardsLoading: false,
      cards: [],
      enabledUpdatingId: null,
      denchiWalletSearch: '',
      denchiEnabledFilter: null,
      denchiEnabledFilterOptions: [
        {label: 'All', value: null},
        {label: 'Enabled', value: true},
        {label: 'Disabled', value: false}
      ],
      denchiNfcFilter: null,
      denchiNfcFilterOptions: [
        {label: 'All', value: null},
        {label: 'Written', value: true},
        {label: 'Unwritten', value: false}
      ],
      denchiPagination: {
        page: 1,
        rowsPerPage: 10,
        rowsNumber: 0
      },
      denchiColumns: [
        {
          name: 'wallet',
          label: 'Wallet',
          field: 'wallet_name',
          align: 'left',
          format: value => value || '—'
        },
        {
          name: 'balance',
          label: 'Balance',
          field: 'balance',
          align: 'right',
          format: value =>
            value === null ? '—' : `${value.toLocaleString()} sats`
        },
        {
          name: 'withdraw_link',
          label: 'Withdraw Link',
          field: 'withdraw_url',
          align: 'left'
        },
        {
          name: 'enabled',
          label: 'Enabled',
          field: 'enabled',
          align: 'left'
        },
        {
          name: 'nfc',
          label: 'NFC',
          field: 'nfc_written',
          align: 'left'
        },
        {
          name: 'created',
          label: 'Created',
          field: 'created_at',
          align: 'left',
          format: value => new Date(value).toLocaleString()
        },
        {name: 'actions', label: 'Actions', field: () => '', align: 'right'}
      ],
      expirationUnits: ['days', 'weeks', 'months'],
      profileColumns: [
        {name: 'name', label: 'Name', field: 'name', align: 'left'},
        {
          name: 'wallet_name_base',
          label: 'Wallet name base',
          field: 'wallet_name_base',
          align: 'left'
        },
        {
          name: 'max_payment_per_use',
          label: 'Max payment per use (sats)',
          field: 'max_payment_per_use',
          align: 'right'
        },
        {
          name: 'expiration',
          label: 'Expiration',
          field: 'expiration_value',
          align: 'left'
        },
        {name: 'actions', label: '', field: 'actions', align: 'right'}
      ],
      profileDialog: {
        show: false,
        saving: false,
        useExpiration: false,
        data: {}
      },
      nfcDialog: {
        show: false,
        denchiId: null,
        lnurlwUri: null,
        writing: false,
        tagWritten: false,
        error: null,
        abortController: null
      },
      deleteDialog: {
        show: false,
        deleting: false,
        denchi: null
      },
      bulkDisableDialog: {
        show: false,
        processing: false
      }
    }
  },
  methods: {
    openBulkDisableDialog() {
      this.bulkDisableDialog.show = true
    },
    closeBulkDisableDialog() {
      if (!this.bulkDisableDialog.processing) {
        this.bulkDisableDialog.show = false
      }
    },
    async disableAllCards() {
      if (this.bulkDisableDialog.processing) {
        return
      }
      this.bulkDisableDialog.processing = true
      try {
        const response = await LNbits.api.request(
          'POST',
          '/denchi/api/v1/cards/disable-all',
          null
        )
        const result = response.data
        this.bulkDisableDialog.show = false
        await this.getCards({pagination: this.denchiPagination})
        Quasar.Notify.create({
          type: result.failed ? 'warning' : 'positive',
          message: `Disabled: ${result.updated}. Already disabled: ${result.skipped}. Failed: ${result.failed}.`
        })
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.bulkDisableDialog.processing = false
      }
    },
    async issueCard() {
      if (!this.issueProfileId || this.issuing) {
        return
      }

      this.issuing = true
      try {
        const response = await LNbits.api.request(
          'POST',
          '/denchi/api/v1/cards/issue',
          null,
          {profile_id: this.issueProfileId}
        )
        this.denchiPagination.page = 1
        await this.getCards({pagination: this.denchiPagination})
        Quasar.Notify.create({
          type: 'positive',
          message: 'Denchi Card issued as Unwritten.'
        })
        await this.prepareNfcWrite(response.data.id)
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.issuing = false
      }
    },
    async getCards(props) {
      if (props && props.pagination) {
        this.denchiPagination = props.pagination
      }

      const pagination = this.denchiPagination
      const params = new URLSearchParams({
        limit: pagination.rowsPerPage,
        offset: (pagination.page - 1) * pagination.rowsPerPage
      })
      if (this.denchiNfcFilter !== null) {
        params.set('nfc_written', this.denchiNfcFilter)
      }
      const walletName = (this.denchiWalletSearch || '').trim()
      if (walletName) {
        params.set('wallet_name', walletName)
      }

      this.cardsLoading = true
      try {
        const response = await LNbits.api.request(
          'GET',
          `/denchi/api/v1/cards?${params.toString()}`,
          null
        )
        this.cards = response.data.data
        this.denchiPagination.rowsNumber = response.data.total
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.cardsLoading = false
      }
    },
    filterCards() {
      this.denchiPagination.page = 1
      this.getCards({pagination: this.denchiPagination})
    },
    async setCardEnabled(denchi) {
      if (this.enabledUpdatingId !== null) {
        return
      }

      this.enabledUpdatingId = denchi.id
      try {
        await LNbits.api.request(
          'PUT',
          `/denchi/api/v1/cards/${denchi.id}/enabled`,
          null,
          {enabled: !denchi.enabled}
        )
        await this.getCards({pagination: this.denchiPagination})
        Quasar.Notify.create({
          type: 'positive',
          message: denchi.enabled
            ? 'Denchi Card disabled.'
            : 'Denchi Card enabled.'
        })
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.enabledUpdatingId = null
      }
    },
    async prepareNfcWrite(denchiId) {
      this.nfcDialog.show = true
      this.nfcDialog.denchiId = denchiId
      this.nfcDialog.lnurlwUri = null
      this.nfcDialog.writing = true
      this.nfcDialog.tagWritten = false
      this.nfcDialog.error = null

      try {
        const response = await LNbits.api.request(
          'GET',
          `/denchi/api/v1/cards/${denchiId}/nfc`,
          null
        )
        this.nfcDialog.lnurlwUri = response.data.lnurlw_uri
        await this.$nextTick()
        await this.writeNfc()
      } catch (error) {
        this.nfcDialog.error = this.nfcErrorMessage(error)
        LNbits.utils.notifyApiError(error)
      } finally {
        this.nfcDialog.writing = false
      }
    },
    async writeNfc() {
      if (!window.isSecureContext) {
        this.nfcDialog.error = 'Web NFC requires a secure HTTPS connection.'
        return
      }
      if (!('NDEFReader' in window)) {
        this.nfcDialog.error =
          'Web NFC is not supported by this browser. Use Android Chrome.'
        return
      }

      this.nfcDialog.writing = true
      this.nfcDialog.error = null
      const controller = new AbortController()
      this.nfcDialog.abortController = controller
      try {
        const reader = new NDEFReader()
        await reader.write(
          {
            records: [{recordType: 'url', data: this.nfcDialog.lnurlwUri}]
          },
          {overwrite: false, signal: controller.signal}
        )
        this.nfcDialog.tagWritten = true
        this.holdPostWriteNfcScan()
        await this.markNfcWritten()
      } catch (error) {
        if (error.name !== 'AbortError') {
          this.nfcDialog.error = this.nfcErrorMessage(error)
          Quasar.Notify.create({
            type: 'negative',
            message: this.nfcDialog.error
          })
        }
      } finally {
        if (this.nfcDialog.abortController === controller) {
          this.nfcDialog.abortController = null
          this.nfcDialog.writing = false
        }
      }
    },
    async holdPostWriteNfcScan() {
      const scanController = new AbortController()
      try {
        const scanReader = new NDEFReader()
        await scanReader.scan({signal: scanController.signal})
        await new Promise(resolve => window.setTimeout(resolve, 2000))
      } catch (error) {
        if (error.name !== 'AbortError') {
          console.warn('Post-write NFC scan could not be started.', error)
        }
      } finally {
        scanController.abort()
      }
    },
    async markNfcWritten() {
      try {
        await LNbits.api.request(
          'POST',
          `/denchi/api/v1/cards/${this.nfcDialog.denchiId}/nfc-written`,
          null
        )
        await this.getCards({pagination: this.denchiPagination})
        this.closeNfcDialog()
        Quasar.Notify.create({
          type: 'positive',
          message: 'NFC card written successfully.'
        })
      } catch (error) {
        this.nfcDialog.error = this.nfcDialog.tagWritten
          ? 'The NFC tag was written, but updating its status failed. Retry to update the status.'
          : this.nfcErrorMessage(error)
        LNbits.utils.notifyApiError(error)
      }
    },
    retryNfcWrite() {
      if (this.nfcDialog.tagWritten) {
        return this.markNfcWritten()
      }
      return this.writeNfc()
    },
    closeNfcDialog() {
      if (this.nfcDialog.abortController) {
        this.nfcDialog.abortController.abort()
      }
      this.nfcDialog.show = false
      this.nfcDialog.denchiId = null
      this.nfcDialog.lnurlwUri = null
      this.nfcDialog.writing = false
      this.nfcDialog.tagWritten = false
      this.nfcDialog.error = null
      this.nfcDialog.abortController = null
    },
    nfcErrorMessage(error) {
      if (error && error.response && error.response.data) {
        return error.response.data.detail || 'NFC operation failed.'
      }
      return (error && error.message) || 'NFC operation failed.'
    },
    openDeleteCardDialog(denchi) {
      this.deleteDialog.denchi = denchi
      this.deleteDialog.show = true
    },
    closeDeleteCardDialog() {
      if (this.deleteDialog.deleting) {
        return
      }
      this.deleteDialog.show = false
      this.deleteDialog.denchi = null
    },
    async deleteCard() {
      const denchi = this.deleteDialog.denchi
      if (!denchi || this.deleteDialog.deleting) {
        return
      }

      this.deleteDialog.deleting = true
      try {
        await LNbits.api.request(
          'DELETE',
          `/denchi/api/v1/cards/${denchi.id}`,
          null
        )
        if (this.cards.length === 1 && this.denchiPagination.page > 1) {
          this.denchiPagination.page -= 1
        }
        this.deleteDialog.show = false
        this.deleteDialog.denchi = null
        await this.getCards({pagination: this.denchiPagination})
        Quasar.Notify.create({
          type: 'positive',
          message: 'Denchi Card, Withdraw link, and wallet deleted.'
        })
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.deleteDialog.deleting = false
      }
    },
    requiredRule(value) {
      return Boolean(value && value.trim()) || 'This field is required.'
    },
    positiveIntegerRule(value) {
      return (
        (Number.isInteger(value) && value > 0) ||
        'Enter a positive whole number.'
      )
    },
    openCreateDialog() {
      this.profileDialog.useExpiration = false
      this.profileDialog.data = {
        name: '',
        wallet_name_base: '',
        max_payment_per_use: null,
        expiration_value: null,
        expiration_unit: 'days'
      }
      this.profileDialog.show = true
    },
    async getProfiles() {
      this.loading = true
      try {
        const response = await LNbits.api.request(
          'GET',
          '/denchi/api/v1/profiles',
          null
        )
        this.profiles = response.data
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.loading = false
      }
    },
    async createProfile() {
      const data = {...this.profileDialog.data}
      if (!this.profileDialog.useExpiration) {
        data.expiration_value = null
        data.expiration_unit = null
      }

      this.profileDialog.saving = true
      try {
        const response = await LNbits.api.request(
          'POST',
          '/denchi/api/v1/profiles',
          null,
          data
        )
        this.profiles.push(response.data)
        this.profiles.sort((a, b) => a.name.localeCompare(b.name))
        this.profileDialog.show = false
        Quasar.Notify.create({type: 'positive', message: 'Profile created.'})
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.profileDialog.saving = false
      }
    },
    confirmDelete(profile) {
      LNbits.utils
        .confirmDialog(`Delete profile "${profile.name}"?`)
        .onOk(() => this.deleteProfile(profile.id))
    },
    async deleteProfile(profileId) {
      try {
        await LNbits.api.request(
          'DELETE',
          `/denchi/api/v1/profiles/${profileId}`,
          null
        )
        this.profiles = this.profiles.filter(
          profile => profile.id !== profileId
        )
        Quasar.Notify.create({type: 'positive', message: 'Profile deleted.'})
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      }
    }
  },
  created() {
    this.getCards({pagination: this.denchiPagination})
    this.getProfiles()
  }
})
