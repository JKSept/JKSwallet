import xrpl
import wx
import asyncio
import re
import wx.dataview
import wx.adv
from threading import Thread
from decimal import Decimal

class XRPLMonitorThread(Thread):
    """
    worker thread that watches ledger for new events and pass info to main frame
    to be shown in the UI
    """
    def __init__(self, url, gui):
        Thread.__init__(self, daemon=True)
        # for safety this thread should treat self.gui as read-only
        # to modify gui use wc.CallAfter(...)
        self.gui = gui
        self.url = url
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.set_debug(True)

    def run(self):
        """
        Never-ending loop thread monitors messages from XRPL and sends them to gui
        handles making requests to XRPL when gui prompts
        """
        self.loop.run_forever()

    async def watch_xrpl_account(self, address, wallet=None):
        """
        task opens connection to XRPL and handles incoming messages
        sending them to appropriate parts of gui
        """
        self.account = address
        self.wallet = wallet

        async with xrpl.asyncio.clients.AsyncWebsocketClient(self.url) as self.client:
            await self.on_connected()
            async for message in self.client:
                mtype = message.get("type")
                if mtype == "ledgerClosed":
                    wx.CallAfter(self.gui.update_ledger, message)
                elif mtype == "transaction":
                    wx.CallAfter(self.gui.add_tx_from_sub, message)
                    response = await self.client.request(xrpl.models.requests.AccountInfo(account=self.account, ledger_index=message["ledger_index"]))
                    wx.CallAfter(self.gui.update_account, response.result["account_data"])

    async def on_connected(self):
        """
        set up subscriptions and populate gui with data from ledger on startup
        requires self.client to be connected first
        """
        # set up subscription for new ledgers
        response = await self.client.request(xrpl.models.requests.Subscribe(streams=["ledger"], accounts=[self.account]))

        wx.CallAfter(self.gui.update_ledger, response.result)

        # get starting values for account info
        response = await self.client.request(xrpl.models.requests.AccountInfo(account=self.account, ledger_index="validated"))
        if not response.is_successful():
            print("Errors from server:", response)
            # error for accounts that don't exist on network
            exit(1)
        wx.CallAfter(self.gui.update_account, response.result["account_data"])

        # get first page of the account's tx history
        response = await self.client.request(xrpl.models.requests.AccountTx(account=self.account))
        wx.CallAfter(self.gui.update_account_tx, response.result)

        if self.wallet:
            wx.CallAfter(self.gui.enable_readwrite)


    async def send_xrp(self, paydata):
        """
        Prepare sign and send XRP payments
        expects dictionary with:
        {
            "dtag": destination tag as string, optional
            "to": destination address (classic or X-address)
            "amt": amount of decimal XRP to send as string
        }
        """
        dtag = paydata.get("dtag", "")
        if dtag.strip() == "":
            dtag = None
        if dtag is not None:
            try:
                dtag = int(dtag)
                if dtag < 0 or dtag > 2**32-1:
                    raise ValueError("Destination tag must be valid 32-bit unsigned integer")
            except ValueError as e:
                print("Invalid destination tag:", e)
                print("Canceled sending payment.")
                return

        tx = xrpl.models.transactions.Payment(
            account=self.account,
            destination=paydata["to"],
            amount=xrpl.utils.xrp_to_drops(paydata["amt"]),
            destination_tag=dtag
        )
        # autofill provides a sequence number but can fail if TX are sent too fast
        tx_signed = await xrpl.asyncio.transaction.safe_sign_and_autofill_transaction(
            tx, self.wallet, self.client)
        await xrpl.asyncio.transaction.submit_transaction(tx_signed, self.client)
        wx.CallAfter(self.gui.add_pending_tx, tx_signed)


class TWaXLFrame(wx.Frame):
    "user interface, main frame"
    def __init__(self, url, test_network=True):
        wx.Frame.__init__(self, None, title="JKSwallet", size=wx.Size(800, 400))

        self.test_network = test_network
        # the ledgers current reserve setting, to be filled later
        self.reserve_base = None
        self.reserve_inc = None

        self.build_ui()

        # pop up asking user for account info
        address, wallet = self.prompt_for_account()
        self.classic_address = address

        # starts background thread for updates from the ledger ----
        self.worker = XRPLMonitorThread(url, self)
        self.worker.start()
        self.run_bg_job(self.worker.watch_xrpl_account(address, wallet))

    def build_ui(self):
        """
        Called during __init__ to set up all gui elements
        """
        self.tabs = wx.Notebook(self, style=wx.BK_DEFAULT)
        # tab 1 : Summery
        main_panel = wx.Panel(self.tabs)
        self.tabs.AddPage(main_panel, "Overview")
        #background color and image testing
        # main_panel.SetBackgroundColour("grey")
        main_panel.image = wx.Image('jkswallet.png', wx.BITMAP_TYPE_PNG)
        main_panel.image = main_panel.image.Rescale(main_panel.image.Width*0.25,
                                                    main_panel.image.Height*0.25,
                                                    wx.IMAGE_QUALITY_HIGH)

        main_panel.bitmap = wx.StaticBitmap(main_panel, -1,
                                            wx.Bitmap(main_panel.image),
                                            pos=(690, 280))


        self.acc_info_area = wx.StaticBox(main_panel, label="Account Info")

        lbl_address = wx.StaticText(self.acc_info_area, label="Classic Address:")
        self.st_classic_address = wx.StaticText(self.acc_info_area, label="TBD")
        lbl_xaddress = wx.StaticText(self.acc_info_area, label="X-Address:")
        self.st_x_address = wx.StaticText(self.acc_info_area, label="TBD")
        lbl_xrp_bal = wx.StaticText(self.acc_info_area, label="XRP Balance:")
        self.st_xrp_balance = wx.StaticText(self.acc_info_area, label="TBD")
        lbl_reserve = wx.StaticText(self.acc_info_area, label="XRP Reserved:")
        self.st_reserve = wx.StaticText(self.acc_info_area, label="TBD")


        # send XRP button. Disabled until we have a secret key & network connection
        self.sxb = wx.Button(main_panel, label="Send XRP")
        self.sxb.SetToolTip("Disabled in read-only mode.")
        self.sxb.Disable()
        self.Bind(wx.EVT_BUTTON, self.click_send_xrp, source=self.sxb)

        aia_sizer = AutoGridBagSizer(self.acc_info_area)
        aia_sizer.BulkAdd( ((lbl_address, self.st_classic_address),
                            (lbl_xaddress, self.st_x_address),
                            (lbl_xrp_bal, self.st_xrp_balance),
                            (lbl_reserve, self.st_reserve)) )

        self.ledger_info = wx.StaticText(main_panel, label="Not Connected...")

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self.acc_info_area, 1, flag=wx.EXPAND|wx.ALL, border=5)
        main_sizer.Add(self.sxb, 0, flag=wx.ALL, border=5)
        main_sizer.Add(self.ledger_info, 1, flag=wx.EXPAND|wx.ALL, border=5)
        main_panel.SetSizer(main_sizer)

        # tab 2: TX history
        objs_panel = wx.Panel(self.tabs)
        self.tabs.AddPage(objs_panel, "TX History")
        objs_sizer = wx.BoxSizer(wx.VERTICAL)
        # objs_panel.SetBackgroundColour("grey")
        self.pending_tx_rows = {} # map pendting tx hashes to rows in the history ui

        self.tx_list = wx.dataview.DataViewListCtrl(objs_panel)
        self.tx_list.AppendTextColumn("Confirmed")
        self.tx_list.AppendTextColumn("Type")
        self.tx_list.AppendTextColumn("From")
        self.tx_list.AppendTextColumn("To")
        self.tx_list.AppendTextColumn("Value Delivered")
        self.tx_list.AppendTextColumn("Identifying Hash")
        self.tx_list.AppendTextColumn("Raw JSON")
        objs_sizer.Add(self.tx_list, 1, wx.EXPAND|wx.ALL)

        objs_panel.SetSizer(objs_sizer)

    def run_bg_job(self, job):
        """
        schedules job to run asynchronously in the XRPL worker thread
        the job should be a Future like from calling a async function
        """
        task = asyncio.run_coroutine_threadsafe(job, self.worker.loop)

    def update_ledger(self, message):
        """
        Process a ledger subscription message to update ui with
        information about latest validated ledger
        """
        close_time_iso = xrpl.utils.ripple_time_to_datetime(message["ledger_time"]).isoformat()
        self.ledger_info.SetLabel(f"Latest validated ledger:\n"
                                  f"Ledger Index: {message['ledger_index']}\n"
                                  f"Ledger Hash: {message['ledger_hash']}\n"
                                  f"Close time: {close_time_iso}")

        self.reserve_base = xrpl.utils.drops_to_xrp(str(message["reserve_base"]))
        self.reserve_inc = xrpl.utils.drops_to_xrp(str(message["reserve_inc"]))

    def prompt_for_account(self):
        """
        prompt user for an account to use in base58-encoded format:
        - master key seed: Grants read-write access
        - classic address: Grants read-only access
        - X-address: Grants read-only access

        exits for error code 1 if user cancles dialog or input doesn't match formats or network

        populates the classic address and X-address labels in ui

        returns (classic_address, wallet) where wallet is None in read-only mode
        """
        account_dialog = wx.TextEntryDialog(self,
                                            "Enter account address or key",
                                            caption="Enter account",
                                            value="")
        account_dialog.Bind(wx.EVT_TEXT, self.toggle_dialog_style)

        if account_dialog.ShowModal() != wx.ID_OK:
            # if the user presses Cancel on the account entry, exit the app.
            exit(1)

        value = account_dialog.GetValue().strip()
        account_dialog.Destroy()

        classic_address = ""
        wallet = None
        x_address = ""

        if xrpl.core.addresscodec.is_valid_xaddress(value):
            x_address = value
            classic_address, dest_tag, test_network = xrpl.core.addresscodec.xaddress_to_classic_address(value)
            if test_network != self.test_network:
                on_net = "TEST NETWORK" if self.test_network else "MAINNET"
                print(f"X-address {value} is meant for a different network type..."
                      f"than this client is connected to."
                      f"Client is on: {on_net}")
                exit(1)

        elif xrpl.core.addresscodec.is_valid_classic_address(value):
            classic_address = value
            x_address = xrpl.core.addresscodec.classic_address_to_xaddress(value, tag=None, is_test_network=self.test_network)

        else:
            try:
                # check if it's a valid seed
                seed_bytes, alg = xrpl.core.addresscodec.decode_seed(value)
                wallet = xrpl.wallet.Wallet(seed=value, sequence=0)
                x_address = wallet.get_xaddress(is_test=self.test_network)
                classic_address = wallet.classic_address
            except Exception as e:
                print(e)
                exit(1)

        # update ui with address value
        self.st_classic_address.SetLabel(classic_address)
        self.st_x_address.SetLabel(x_address)

        return classic_address, wallet

    def toggle_dialog_style(self, event):
        """
        Automatically switches to a password style dialog if it looks like user is entering seed
        """
        dlg = event.GetEventObject()
        v = dlg.GetValue().strip()
        if v[:1] == "s":
            dlg.SetWindowStyle(wx.TE_PASSWORD)
        else:
            dlg.SetWindowStyle(wx.TE_LEFT)

    def calculate_reserve_xrp(self, owner_count):
        """
        Calculates how mcuh XRP the user needs to reserve based on account's
        owner count and the reserve values in latest ledger
        """
        if self.reserve_base == None or self.reserve_inc == None:
            return None
        oc_decimal = Decimal(owner_count)
        reserve_xrp = self.reserve_base + (self.reserve_inc * oc_decimal)
        return reserve_xrp

    def update_account(self, acct):
        """
        Updates the account info ui based on account_info response
        """
        xrp_balance = str(xrpl.utils.drops_to_xrp(acct["Balance"]))
        self.st_xrp_balance.SetLabel(xrp_balance)

        # displays account reserve
        reserve_xrp = self.calculate_reserve_xrp(acct.get("OwnerCount", 0))
        if reserve_xrp != None:
            self.st_reserve.SetLabel(str(reserve_xrp))


    def displayable_amount(self, a):
        """
        Convert amount value from XRPL to a string to be displayed
        - convert drops to XRP to 6 decimals
        - for issued tokens, show amount, currency codem issuer
        leaves non-standard (hex) currency as is
        """
        if a == "unavailable":
            # special code for pre-2014 partial payments
            return a
        elif type(a) == str:
            # its an xrp amount in drops, convert to decimal
            return f"{xrpl.utils.drops_to_xrp(a)} XRP"
        else:
            # token amount
            return f"{a['value']} {a['currency']}.{a['issuer']}"


    def add_tx_row(self, t, prepend=False):
        """
        Add one row to the account TX history control
        Helper function called by other methods
        """
        conf_dt = xrpl.utils.ripple_time_to_datetime(t['tx']['date'])
        # convert datetime to locale-default and timezone
        confirmation_time = conf_dt.astimezone().strftime("%c")

        tx_hash = t["tx"]["hash"]
        tx_type = t["tx"]["TransactionType"]
        from_acct = t["tx"].get("Account") or ""
        if from_acct == self.classic_address:
            from_acct = "(Me)"
        to_acct = t["tx"].get("Destination") or ""
        if to_acct == self.classic_address:
            to_acct = "(Me)"

        delivered_amt = t["meta"].get("delivered_amount")
        if delivered_amt:
            delivered_amt = self.displayable_amount(delivered_amt)
        else:
            delivered_amt = ""

        cols = (confirmation_time, tx_type, from_acct, to_acct, delivered_amt, tx_hash, str(t))
        if prepend:
            self.tx_list.PrependItem(cols)
        else:
            self.tx_list.AppendItem(cols)


    def update_account_tx(self, data):
        """
        update the TX history tab with info from account_tx response
        """
        txs = data["transactions"]
        # NOTE if code is extended to have paginated responses would be useful to keep
        # previous history inside of deleting first
        self.tx_list.DeleteAllItems()
        for t in txs:
            self.add_tx_row(t)


    def add_tx_from_sub(self, t):
        """add 1 TX to the history based on a subscription stream message
        assumes only validated transaction streams
        not proposed

        also send a notification about it
        """
        # convert to same format as account_tx results
        t["tx"] = t["transaction"]

        if t["tx"]["hash"] in self.pending_tx_rows.keys():
            dvi = self.pending_tx_rows[t["tx"]["hash"]]
            pending_row = self.tx_list.ItemToRow(dvi)
            self.tx_list.DeleteItem(pending_row)

        self.add_tx_row(t, prepend=True)
        # scroll to top of list
        self.tx_list.EnsureVisible(self.tx_list.RowToItem(0))

        # send notification message, aka toast, about TX
        # note TX stream and account_tx for all TX that affect account
        notif = wx.adv.NotificationMessage(title="New Transaction", message = f"New {t['tx']['TransactionType']} transaction confirmed.")
        notif.SetFlags(wx.ICON_INFORMATION)
        notif.Show()

    def enable_readwrite(self):
        """
        Enable buttons for sending transactions
        """
        self.sxb.Enable()
        self.sxb.SetToolTip("")

    def click_send_xrp(self, event):
        """
        Pop up a dialog for the user to input how much XRP
        to send where and send the TX (if user doesn't cancle
        """
        dlg = SendXRPDialog(self)
        dlg.CenterOnScreen()
        resp = dlg.ShowModal()
        if resp !=wx.ID_OK:
            print("Send XRP canceled")
            dlg.Destroy()
            return

        paydata = dlg.get_payment_data()
        dlg.Destroy()
        self.run_bg_job(self.worker.send_xrp(paydata))
        notif = wx.adv.NotificationMessage(title="Sending...", message =
                f"Sending a payment for {paydata['amt']} XRP...")
        notif.SetFlags(wx.ICON_INFORMATION)
        notif.Show()

    def add_pending_tx(self, txm):
        """
        Add a 'pending' tx to this history based on a transaction model
        that was just submitted
        """
        confirmation_time = "(pending)"
        tx_type = txm.transaction_type
        from_acct = txm.account
        if from_acct == self.classic_address:
            from_acct = "(Me)"
        # some tx don't have destination so we need to handle that
        to_acct = getattr(txm, "destination", "")
        if to_acct == self.classic_address:
            to_acct = "(Me)"
        # delivered amount is only known after a tx is processed
        # so leave this column empty in the display for pending txs
        delivered_amt = ""
        tx_hash = txm.get_hash()
        cols = (confirmation_time, tx_type, from_acct, to_acct, delivered_amt,
                tx_hash, str(txm.to_xrpl()))
        self.tx_list.PrependItem(cols)
        self.pending_tx_rows[tx_hash] = self.tx_list.RowToItem(0)



class SendXRPDialog(wx.Dialog):
    """
    Pop up dialog that prompts user for information necessary to send direct
    XRP-to-XRP payments on the XRPL
    """
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent, title="Send XRP")
        sizer = AutoGridBagSizer(self)
        self.parent = parent

        lbl_to = wx.StaticText(self, label="To (Address):")
        lbl_dtag = wx.StaticText(self, label="Destination Tag:")
        lbl_amt = wx.StaticText(self, label="Amount of XRP:")
        self.txt_to = wx.TextCtrl(self)
        self.txt_dtag = wx.TextCtrl(self)
        self.txt_amt = wx.SpinCtrlDouble(self, value="20.0", min=0.000001)
        self.txt_amt.SetDigits(6)
        self.txt_amt.SetIncrement(1.0)

        # the "send" button functionally an "ok" button for the text
        self.btn_send = wx.Button(self, wx.ID_OK, label="SEND")
        btn_cancel = wx.Button(self, wx.ID_CANCEL)

        sizer.BulkAdd(((lbl_to, self.txt_to),
                       (lbl_dtag, self.txt_dtag),
                       (lbl_amt, self.txt_amt),
                       (btn_cancel, self.btn_send)) )
        sizer.Fit(self)

        self.txt_dtag.Bind(wx.EVT_TEXT, self.on_dest_tag_edit)
        self.txt_to.Bind(wx.EVT_TEXT, self.on_to_edit)

    def get_payment_data(self):
        """
        Construct a dictionary with the relevant payment details
        to pass to the worker thread for making payment
        called after the user clicks "send"
        """
        return {
            "to": self.txt_to.GetValue().strip(),
            "dtag": self.txt_dtag.GetValue().strip(),
            "amt": self.txt_amt.GetValue(),
        }

    def on_to_edit(self, event):
        """
        When user edits the "To" field check that the address valid
        """
        v = self.txt_to.GetValue().strip()

        if not (xrpl.core.addresscodec.is_valid_classic_address(v) or
                xrpl.core.addresscodec.is_valid_xaddress(v) ):
            self.btn_send.Disable()
        elif v == self.parent.classic_address:
            self.btn_send.Disable()
        else:
            self.btn_send.Enable()


    def on_dest_tag_edit(self, event):
        """
        When user edits the Destination Tag field,
        strip non-numeric characters from it
        """
        v = self.txt_dtag.GetValue().strip()
        v = re.sub(r"[^0-9]", "", v)
        self.txt_dtag.ChangeValue(v) #SetValue would generate another EVT_TEXT
        self.txt_dtag.SetInsertionPointEnd()



class AutoGridBagSizer(wx.GridBagSizer):
    """
    helper class for adding a bunch of uniform items to a GridBagSizer
    """
    def __init__(self, parent):
        wx.GridBagSizer.__init__(self, vgap=5, hgap=5)
        self.parent = parent

    def BulkAdd(self, ctrls):
        """
        Given a two-dimenson iterable 'ctrls', add all the items in a grid
        top to bottom, left to right, with each iterable being a row
        set total columns based on longest iterable
        """
        flags = wx.EXPAND|wx.ALL|wx.RESERVE_SPACE_EVEN_IF_HIDDEN|wx.ALIGN_CENTER_VERTICAL
        for x, row in enumerate(ctrls):
            for y, ctrl in enumerate(row):
                self.Add(ctrl, (x, y), flag=flags, border=5)
        self.parent.SetSizer(self)

if __name__ == "__main__":
    WS_URL = "wss://s.altnet.rippletest.net:51233" # TESTNET
    app = wx.App()
    frame = TWaXLFrame(WS_URL, test_network=True)
    frame.Show()
    app.MainLoop()