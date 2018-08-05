import os
import pytest
import time
from pysqlcipher3 import dbapi2 as sqlcipher
from eth_utils.address import to_checksum_address

from rotkehlchen.utils import ts_now, createTimeStamp
from rotkehlchen.db.dbhandler import (
    ROTKEHLCHEN_DB_VERSION,
    DEFAULT_START_DATE,
    DEFAULT_UI_FLOATING_PRECISION,
    DEFAULT_BALANCE_SAVE_FREQUENCY,
)
from rotkehlchen.data_handler import DataHandler
from rotkehlchen.errors import AuthenticationError, InputError
from rotkehlchen.constants import YEAR_IN_SECONDS


TABLES_AT_INIT = [
    'timed_balances',
    'timed_location_data',
    'user_credentials',
    'blockchain_accounts',
    'multisettings',
    'current_balances',
    'trades',
    'settings',
]


def test_data_init_and_password(data_dir, username):
    """DB Creation logic and tables at start testing"""
    # Creating a new data dir should work
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=True)
    assert os.path.exists(os.path.join(data_dir, username))

    # Trying to re-create it should throw
    with pytest.raises(AuthenticationError):
        data.unlock(username, '123', create_new=True)

    # Trying to unlock a non-existing user without create_new should throw
    with pytest.raises(AuthenticationError):
        data.unlock('otheruser', '123', create_new=False)

    # now relogin and check all tables are there
    del data
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=False)
    cursor = data.db.conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    results = cursor.fetchall()
    results = [result[0] for result in results]
    assert set(results) == set(TABLES_AT_INIT)

    # finally logging in with wrong password should also fail
    del data
    data = DataHandler(data_dir)
    with pytest.raises(AuthenticationError):
        data.unlock(username, '1234', create_new=False)


def test_export_import_db(data_dir, username):
    """Create a DB, write some data and then after export/import confirm it's there"""
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=True)
    data.set_fiat_balance('EUR', 10)

    encoded_data, data_hash = data.compress_and_encrypt_db('123')
    # The server would return them decoded
    encoded_data = encoded_data.decode()
    data.decompress_and_decrypt_db('123', encoded_data)
    fiat_balances = data.get_fiat_balances()
    assert len(fiat_balances) == 1
    assert int(fiat_balances['EUR']) == 10


def test_writting_fetching_data(data_dir, username):
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=True)

    tokens = ['GNO', 'RDN']
    data.write_owned_eth_tokens(tokens)
    result = data.db.get_owned_tokens()
    assert set(tokens) == set(result)

    data.add_blockchain_account('BTC', '1CB7Pbji3tquDtMRp8mBkerimkFzWRkovS')
    data.add_blockchain_account('ETH', '0xd36029d76af6fE4A356528e4Dc66B2C18123597D')
    data.add_blockchain_account('ETH', '0x80b369799104a47e98a553f3329812a44a7facdc')
    accounts = data.db.get_blockchain_accounts()
    assert len(accounts) == 2
    assert accounts['BTC'] == ['1CB7Pbji3tquDtMRp8mBkerimkFzWRkovS']
    assert set(accounts['ETH']) == set([
        '0xd36029d76af6fE4A356528e4Dc66B2C18123597D',
        to_checksum_address('0x80b369799104a47e98a553f3329812a44a7facdc')

    ])
    # Add existing account should fail
    with pytest.raises(sqlcipher.IntegrityError):
        data.add_blockchain_account('ETH', '0xd36029d76af6fE4A356528e4Dc66B2C18123597D')
    # Remove non-existing account
    with pytest.raises(InputError):
        data.remove_blockchain_account('ETH', '0x136029d76af6fE4A356528e4Dc66B2C18123597D')
    # Remove existing account
    data.remove_blockchain_account('ETH', '0xd36029d76af6fE4A356528e4Dc66B2C18123597D')
    accounts = data.db.get_blockchain_accounts()
    assert accounts['ETH'] == [to_checksum_address('0x80b369799104a47e98a553f3329812a44a7facdc')]

    result, _ = data.add_ignored_asset('DAO')
    assert result
    result, _ = data.add_ignored_asset('DOGE')
    assert result
    result, _ = data.add_ignored_asset('DOGE')
    assert not result
    assert set(data.db.get_ignored_assets()) == set(['DAO', 'DOGE'])
    result, _ = data.remove_ignored_asset('XXX')
    assert not result
    result, _ = data.remove_ignored_asset('DOGE')
    assert result
    assert data.db.get_ignored_assets() == ['DAO']

    # With nothing inserted in settings make sure default values are returned
    result = data.db.get_settings()
    last_write_diff = ts_now() - result['last_write_ts']
    # make sure last_write was within 3 secs
    assert last_write_diff >= 0 and last_write_diff < 3
    del result['last_write_ts']
    assert result == {
        'historical_data_start': DEFAULT_START_DATE,
        'eth_rpc_port': '8545',
        'ui_floating_precision': DEFAULT_UI_FLOATING_PRECISION,
        'db_version': ROTKEHLCHEN_DB_VERSION,
        'include_crypto2crypto': True,
        'taxfree_after_period': YEAR_IN_SECONDS,
        'balance_save_frequency': DEFAULT_BALANCE_SAVE_FREQUENCY,
        'last_balance_save': 0,
    }

    # Check setting non-existing settings. Should be ignored
    _, msg = data.set_settings({'nonexisting_setting': 1}, accountant=None)
    assert msg != '' and 'nonexisting_setting' in msg
    _, msg = data.set_settings({
        'nonexisting_setting': 1,
        'eth_rpc_port': '8555',
        'ui_floating_precision': 3,
    }, accountant=None)
    assert msg != '' and 'nonexisting_setting' in msg

    # Now check nothing funny made it in the db
    result = data.db.get_settings()
    assert result['eth_rpc_port'] == '8555'
    assert result['ui_floating_precision'] == 3
    assert 'nonexisting_setting' not in result


def from_otc_trade(trade):
    ts = createTimeStamp(trade['otc_timestamp'], formatstr='%d/%m/%Y %H:%M')
    new_trade = {
        'timestamp': ts,
        'location': 'external',
        'pair': trade['otc_pair'],
        'type': trade['otc_type'],
        'amount': str(trade['otc_amount']),
        'rate': str(trade['otc_rate']),
        'fee': str(trade['otc_fee']),
        'fee_currency': trade['otc_fee_currency'],
        'link': trade['otc_link'],
        'notes': trade['otc_notes'],
    }
    if 'otc_id' in trade:
        new_trade['id'] = trade['otc_id']

    return new_trade


def test_writting_fetching_external_trades(data_dir, username):
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=True)

    # add 2 trades and check they are in the DB
    trade1 = {
        'otc_timestamp': '10/03/2018 23:30',
        'otc_pair': 'ETH_EUR',
        'otc_type': 'buy',
        'otc_amount': '10',
        'otc_rate': '100',
        'otc_fee': '0.001',
        'otc_fee_currency': 'ETH',
        'otc_link': 'a link',
        'otc_notes': 'a note',
    }
    trade2 = {
        'otc_timestamp': '10/03/2018 23:35',
        'otc_pair': 'ETH_EUR',
        'otc_type': 'buy',
        'otc_amount': '5',
        'otc_rate': '100',
        'otc_fee': '0.001',
        'otc_fee_currency': 'ETH',
        'otc_link': 'a link 2',
        'otc_notes': 'a note 2',
    }
    result, _, = data.add_external_trade(trade1)
    assert result
    result, _ = data.add_external_trade(trade2)
    assert result
    result = data.get_external_trades()
    del result[0]['id']
    assert result[0] == from_otc_trade(trade1)
    del result[1]['id']
    assert result[1] == from_otc_trade(trade2)

    # edit a trade and check the edit made it in the DB
    trade1['otc_rate'] = '120'
    trade1['otc_id'] = 1
    result, _ = data.edit_external_trade(trade1)
    assert result
    result = data.get_external_trades()
    assert result[0] == from_otc_trade(trade1)
    del result[1]['id']
    assert result[1] == from_otc_trade(trade2)

    # try to edit a non-existing trade
    trade1['otc_rate'] = '160'
    trade1['otc_id'] = 5
    result, _ = data.edit_external_trade(trade1)
    assert not result
    trade1['otc_rate'] = '120'
    trade1['otc_id'] = 1
    result = data.get_external_trades()
    assert result[0] == from_otc_trade(trade1)
    del result[1]['id']
    assert result[1] == from_otc_trade(trade2)

    # try to delete non-existing trade
    result, _ = data.delete_external_trade(6)
    assert not result

    # delete an external trade
    result, _ = data.delete_external_trade(1)
    result = data.get_external_trades()
    del result[0]['id']
    assert result[0] == from_otc_trade(trade2)


def test_upgrade_db_1_to_2(data_dir, username):
    """Test upgrading the DB from version 1 to version 2"""
    # Creating a new data dir should work
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=True)
    # Manually set to version 1 and input a non checksummed account
    cursor = data.db.conn.cursor()
    cursor.execute(
        'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
        ('version', str(1))
    )
    data.db.conn.commit()
    data.db.add_blockchain_account('ETH', '0xe3580c38b0106899f45845e361ea7f8a0062ef12')

    # now relogin and check that the account has been re-saved as checksummed
    del data
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=False)
    accounts = data.db.get_blockchain_accounts()
    assert accounts['ETH'][0] == '0xe3580C38B0106899F45845E361EA7F8a0062Ef12'
    assert data.db.get_version() == ROTKEHLCHEN_DB_VERSION


def test_settings_entry_types(data_dir, username):
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=True)

    data.db.set_settings({
        'version': 1,
        'last_write_ts': 1,
        'premium_should_sync': True,
        'include_crypto2crypto': True,
        'last_data_upload_ts': 1,
        'ui_floating_precision': 1,
        'taxfree_after_period': 1,
        'historical_data_start': '01/08/2015',
        'eth_rpc_port': '8545',
        'balance_save_frequency': 24,
    })

    res = data.db.get_settings()
    assert isinstance(res['db_version'], int)
    assert isinstance(res['last_write_ts'], int)
    assert isinstance(res['premium_should_sync'], bool)
    assert isinstance(res['include_crypto2crypto'], bool)
    assert isinstance(res['ui_floating_precision'], int)
    assert isinstance(res['taxfree_after_period'], int)
    assert isinstance(res['historical_data_start'], str)
    assert isinstance(res['eth_rpc_port'], str)
    assert isinstance(res['balance_save_frequency'], int)
    assert isinstance(res['last_balance_save'], int)


def test_balance_save_frequency_check(data_dir, username):
    data = DataHandler(data_dir)
    data.unlock(username, '123', create_new=True)

    now = int(time.time())
    data_save_ts = now - 24 * 60 * 60 + 20
    data.db.add_multiple_location_data([(
        data_save_ts, 'kraken', '1500',
    )])

    assert not data.should_save_balances()
    data.db.set_settings({'balance_save_frequency': 5})
    assert data.should_save_balances()

    last_save_ts = data.db.get_last_balance_save_time()
    assert last_save_ts == data_save_ts
