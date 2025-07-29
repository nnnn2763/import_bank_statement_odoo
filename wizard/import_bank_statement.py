# -*- coding: utf-8 -*-
###############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Akhil Ashok (odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU LESSER
#    GENERAL PUBLIC LICENSE (LGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3) for more details.
#
#    You should have received a copy of the GNU LESSER GENERAL PUBLIC LICENSE
#    (LGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
import base64
import csv
import os
from datetime import datetime
from io import StringIO
from odoo import fields, models, _
from odoo.exceptions import ValidationError


class ImportBankStatement(models.TransientModel):
    _name = "import.bank.statement"
    _description = "Import button"
    _rec_name = "file_name"

    attachment = fields.Binary(string="Файл", required=True)
    file_name = fields.Char(string="Название файла")
    journal_id = fields.Many2one('account.journal', string="ID журнала")

    def action_statement_import(self):
        split_tup = os.path.splitext(self.file_name)
        if split_tup[1] != '.txt':
            raise ValidationError(_("Поддерживаются только TXT файлы или выписки формата SWIFT"))
        
        try:
            file_content = base64.b64decode(self.attachment)
            file_string = file_content.decode('cp866')
            kind = self._detect_statement_type(file_string)
            if kind == 'txt':
                rows = self._parse_txt_statement(file_string)
            elif kind == 'swift':
                rows = self._parse_swift_statement(file_string)
        except Exception as e:
            raise ValidationError(_("Ошибка при прочтении TXT файла: %s") % str(e))
        
        statements_created = []
        duplicates = 0
        # sort rows by ascending date
        rows.sort(key=lambda x:x['date'])
        
        for row in rows:
            try:
                # extract required fields from the csv structure
                transaction_date = datetime.strptime(row['date'], '%y%m%d').date() if row.get('date') else fields.date.today()
                amount = float(row['sum_byn'].replace(',', '.')) if row.get('sum_byn') else 0.0
                currency = row.get('currency', '')
                beneficiary_account = row.get('beneficiary_account', '')
                beneficiary_bank_code = row.get('beneficiary_bank_code', '')
                beneficiary_name = row.get('beneficiary_name', '')
                payment_purpose = row.get('payment_purpose', '')
                document_id = row.get('document_id', '')
                tax_id = row.get('payer_tax_id', '')

                existing_line = self.env['account.bank.statement.line'].search([
                    ('date', '=', transaction_date),
                    ('amount', '=', amount),
                    ('narration', 'ilike', beneficiary_account)
                ])

                if document_id and existing_line:
                    existing_line = existing_line.filtered(
                        lambda l: document_id in (l.payment_ref or '')
                    )

                
                #print(transaction_date, previous_statement.date, previous_statement.balance_start, balance_start)
                                                
                if existing_line:
                    duplicates += 1
                    continue  # skip this transaction 
                
                # determine if debit or credit
                if row.get('debit_credit') == '1' or row.get('debit_credit') == 'D':  # assuming 1 = debit, 0 = credit
                    amount = -abs(amount)
                else:
                    amount = abs(amount)
                
                # find or create partner bank account
                partner = None
                if tax_id:
                    partner = self.env['res.partner'].search([
                        ('vat', '=', tax_id)
                    ], limit=1)
                
                if not partner:
                    # create partner with tax id
                    partner_name = beneficiary_name or f"Партнёр {tax_id or beneficiary_account}"
                    partner = self.env['res.partner'].create({
                        'name': partner_name,
                        'vat': tax_id,
                        'is_company': True,
                        'customer_rank': 1,
                    })
                                
                # find or create bank account for this partner
                partner_bank = self.env['res.partner.bank'].search([
                    ('acc_number', '=', beneficiary_account),
                    ('partner_id', '=', partner.id)
                ], limit=1)
                                
                if not partner_bank:
                    partner_bank = self.env['res.partner.bank'].create({
                        'acc_number': beneficiary_account,
                        'partner_id': partner.id,
                        'bank_name': beneficiary_bank_code or 'Неизвестный банк',
                        'journal_id': None
                    })
                                
                partner_id = partner.id
                
                # create bank statement
                statement = self.env['account.bank.statement'].create({
                    'name': f"Импорт {beneficiary_account}",
                    #'balance_start': balance_end,
                    'line_ids': [
                        (0, 0, {
                            'date': transaction_date,
                            'payment_ref': f"{document_id} - {payment_purpose}" or 'csv import',
                            'partner_id': partner_id,
                            'journal_id': self.journal_id.id,
                            'amount': amount,
                            'narration': f"Валюта: {currency}, Аккаунт: {beneficiary_account}",
                        }),
                    ],
                })
                previous_statement = self.env['account.bank.statement'].search(
                    [
                        ('journal_id', '=', self.journal_id.id),
                        ('first_line_index', '<', statement.first_line_index)
                    ],
                    limit=1,
                    order='first_line_index DESC',
                )
                balance_end = previous_statement.balance_end_real or 0.0
                statement.balance_start = balance_end
                
                statement._compute_balance_end()
                statements_created.append(statement.id)
                
            except (ValueError, KeyError) as e:
                print(e)
                continue  # skip malformed rows

        #if duplicates > 0:
        #    raise ValidationError(_("Найдено %d дубликатов. Импорт отменён") % duplicates)
        
        if not statements_created and not duplicates:
            raise ValidationError(_("Не найдены верные транзакции в файле"))
        
        return {
            'type': 'ir.actions.act_window',
            'name': 'Импортированная выписка',
            'view_mode': 'tree',
            'res_model': 'account.bank.statement',
            'domain': [('id', 'in', statements_created)],
        }

    def _create_payment_from_statement_line(self, statement_line, partner):
        """create actual payment record from statement line"""
        payment_method = self.env.ref('account.account_payment_method_manual_in') if statement_line.amount > 0 else self.env.ref('account.account_payment_method_manual_out')
        
        payment_vals = {
            'payment_type': 'inbound' if statement_line.amount > 0 else 'outbound',
            'partner_type': 'customer' if statement_line.amount > 0 else 'supplier',
            'partner_id': partner.id,
            'amount': abs(statement_line.amount),
            'journal_id': self.journal_id.id,
            'date': statement_line.date,
            'ref': statement_line.payment_ref,
            'payment_method_id': payment_method.id,
        }
        
        payment = self.env['account.payment'].create(payment_vals)
        payment.action_post()  # post the payment to create journal entries
        
        # link the payment to the statement line
        statement_line.write({
            'payment_id': payment.id,
            'is_reconciled': True,
        })
        
        return payment

    def _parse_txt_statement(self, statement):
        transactions = []
        for line in statement.splitlines():
            # first column always empty
            columns = line.split('*')[1:]
            colType = int(columns[0])
            #print(colType)
        
            if colType == 1:
                data = {
                    #"type": colType,
                    "date": columns[1],
                    "client_account": columns[2],
                    "currency": columns[3],
                    "beneficiary_bank_code": columns[4],
                    "beneficiary_account": columns[5],
                    "beneficiary_currency": columns[6],
                    "beneficiary_name": "",
                    "payment_purpose_code": columns[7],
                    "payment_code": columns[8],
                    "reserved": columns[9],
                    "document_type": columns[10],
                    "document_id": columns[11],
                    "payer_tax_id": columns[12],
                    "tax_id_of_whom_paid": columns[13],
                    "debit_credit": columns[14],
                    "sum": columns[15],
                    "exchange_rate": columns[16],
                    "sum_byn": columns[17],
                    "payment_purpose": columns[18]
                }
                transactions.append(data)

        return transactions

    def _parse_swift_statement(self, statement):
        reading_transaction = False
        transactions = []
        
        for line in statement.splitlines():
            if line.startswith(":61:") and not reading_transaction:
                # formatting:
                # :61:xxxxxxDyyy,yyNTRFzzz//
                # where x = date (first 6 digits in ddmmyy format)
                #       D/C = debit/credit
                #       y = sum (any amount of digits)
                #       z = document number (three digits?)
        
                data = {
                        #"type": colType,
                        "date": "",
                        "client_account": "",
                        "currency": "",
                        "beneficiary_bank_code": "",
                        "beneficiary_account": "",
                        "beneficiary_currency": "",
                        "beneficiary_name": "",
                        "payment_purpose_code": "",
                        "payment_code": "",
                        "reserved": "",
                        "document_type": "",
                        "document_id": "",
                        "payer_tax_id": "",
                        "tax_id_of_whom_paid": "",
                        "debit_credit": "",
                        "sum": "",
                        "exchange_rate": "",
                        "sum_byn": "",
                        "payment_purpose": ""
                }
        
                reading_transaction = True
                line = line[4:]
        
                data["date"] = line[:6]
                data["debit_credit"] = line[6]
                data["sum_byn"], data["document_id"] = line[7:-2].split("NTRF")
        
            if reading_transaction:
                kind = line[:4] if line.startswith(':') else line[:3]
                line = line[4:] if kind.startswith(':') else line[3:]
                match kind:
                    case "?00":
                        data["payment_purpose"] = line
                    case "?10":
                        data["payer_tax_id"] = line
                    case "?30":
                        data["beneficiary_bank_code"] = line
                    case "?31":
                        data["beneficiary_account"] = line
                    case "?32":
                        data["beneficiary_name"] = line
                        transactions.append(data)
                        reading_transaction = False

        return transactions

    def _detect_statement_type(self, data):
        lines = data.splitlines()
        if lines[0][0] == '*':
            return 'txt'
        elif lines[0].startswith(":20:"):
            return 'swift'
    
