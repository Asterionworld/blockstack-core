#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstore
    ~~~~~
    copyright: (c) 2014 by Halfmoon Labs, Inc.
    copyright: (c) 2015 by Blockstack.org
    
    This file is part of Blockstore
    
    Blockstore is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.
    
    Blockstore is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstore.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import logging
import os
import sys
import subprocess
import signal
import json
import datetime
import traceback
import httplib
import time
import socket
import math
import random
import shutil
import tempfile
import binascii
import copy

from ConfigParser import SafeConfigParser

import pybitcoin
from txjsonrpc.netstring import jsonrpc

from lib import nameset as blockstore_state_engine
from lib import get_db_state
from lib.config import REINDEX_FREQUENCY, DEFAULT_DUST_FEE
from lib import *

import lib.nameset.virtualchain_hooks as virtualchain_hooks
import lib.config as config

import virtualchain 
log = virtualchain.session.log 

# global variables, for use with the RPC server and the twisted callback
blockstore_opts = None
bitcoind = None
bitcoin_opts = None
utxo_opts = None
blockchain_client = None 
blockchain_broadcaster = None
indexer_pid = None

def get_bitcoind( new_bitcoind_opts=None, reset=False, new=False ):
   """
   Get or instantiate our bitcoind client.
   Optionally re-set the bitcoind options.
   """
   global bitcoind 
   global bitcoin_opts 
   
   if reset:
       bitcoind = None
   
   elif not new and bitcoind is not None:
      return bitcoind 
   
   if new or bitcoind is None:
      if new_bitcoind_opts is not None:
         bitcoin_opts = new_bitcoind_opts
      
      new_bitcoind = None
      try:
         new_bitcoind = virtualchain.connect_bitcoind( bitcoin_opts )
         
         if new:
             return new_bitcoind
         
         else:
             # save for subsequent reuse 
             bitcoind = new_bitcoind
             return bitcoind 
      
      except Exception, e:
         log.exception( e )
         return None 
      
      
def get_bitcoin_opts():
   """
   Get the bitcoind connection arguments.
   """
   
   global bitcoin_opts 
   return bitcoin_opts 


def get_utxo_opts():
   """
   Get UTXO provider options.
   """
   global utxo_opts
   return utxo_opts


def get_blockstore_opts():
   """
   Get blockstore configuration options.
   """
   global blockstore_opts 
   return blockstore_opts 


def set_bitcoin_opts( new_bitcoin_opts ):
   """
   Set new global bitcoind operations
   """
   global bitcoin_opts 
   bitcoin_opts = new_bitcoin_opts
   
   
def set_utxo_opts( new_utxo_opts ):
   """
   Set new global chian.com options 
   """
   global utxo_opts 
   utxo_opts = new_utxo_opts
   
   
def get_pidfile_path():
   """
   Get the PID file path.
   """
   working_dir = virtualchain.get_working_dir()
   pid_filename = blockstore_state_engine.get_virtual_chain_name() + ".pid"
   return os.path.join( working_dir, pid_filename )


def get_tacfile_path( testset=False ):
   """
   Get the TAC file path for our service endpoint.
   Should be in the same directory as this module.
   """
   working_dir = os.path.abspath(os.path.dirname(__file__))
   tac_filename = ""
   
   if testset:
      tac_filename = blockstore_state_engine.get_virtual_chain_name() + "-testset.tac"
   else:
      tac_filename = blockstore_state_engine.get_virtual_chain_name() + ".tac"
      
   return os.path.join( working_dir, tac_filename )


def get_logfile_path():
   """
   Get the logfile path for our service endpoint.
   """
   working_dir = virtualchain.get_working_dir()
   logfile_filename = blockstore_state_engine.get_virtual_chain_name() + ".log"
   return os.path.join( working_dir, logfile_filename )


def get_state_engine():
   """
   Get a handle to the blockstore virtual chain state engine.
   """
   return get_db_state()
   
   
def get_index_range():
    """
    Get the bitcoin block index range.
    Mask connection failures with timeouts.
    Always try to reconnect.
    
    The last block will be the last block to search for names.
    This will be NUM_CONFIRMATIONS behind the actual last-block the 
    cryptocurrency node knows about.
    """
    
    bitcoind_session = get_bitcoind( new=True )
    
    first_block = None 
    last_block = None
    while last_block is None:

        first_block, last_block = virtualchain.get_index_range( bitcoind_session )

        if last_block is None:
            
            # try to reconnnect 
            time.sleep(1)
            log.error("Reconnect to bitcoind")
            bitcoind_session = get_bitcoind( new=True )
            continue 
        
        else:
            return first_block, last_block - NUM_CONFIRMATIONS
        

def die_handler_server(signal, frame):
    """
    Handle Ctrl+C for server subprocess
    """
    
    log.info('Exiting blockstored server')
    stop_server()
    sys.exit(0)



def die_handler_indexer(signal, frame):
    """
    Handle Ctrl+C for indexer processe
    """
    
    db = get_state_engine()
    virtualchain.stop_sync_virtualchain( db )
    sys.exit(0)


def json_traceback():
    exception_data = traceback.format_exc().splitlines()
    return {
        "error": exception_data[-1],
        "traceback": exception_data
    }

 
def get_utxo_provider_client():
   """
   Get or instantiate our blockchain UTXO provider's client.
   Return None if we were unable to connect
   """
   
   # acquire configuration (which we should already have)
   blockstore_opts, bitcoin_opts, utxo_opts, dht_opts = configure( interactive=False )
   
   try:
       utxo_provider = connect_utxo_provider( utxo_opts )
       return utxo_provider
   except Exception, e:
       log.exception(e)
       return None 


def get_tx_broadcaster():
   """
   Get or instantiate our blockchain UTXO provider's transaction broadcaster.
   fall back to the utxo provider client, if one is not designated
   """
   
   # acquire configuration (which we should already have)
   blockstore_opts, blockchain_opts, utxo_opts, dht_opts = configure( interactive=False )
   
   # is there a particular blockchain client we want for importing?
   if 'tx_broadcaster' not in blockstore_opts:
       return get_utxo_provider_client() 
   
   broadcaster_opts = default_utxo_provider_opts( blockstore_opts['tx_broadcaster'] ) 
       
   try:
       blockchain_broadcaster = connect_utxo_provider( broadcaster_opts )
       return blockchain_broadcaster
   except:
       log.exception(e)
       return None 



def get_name_cost( name ):
    """
    Get the cost of a name, given the fully-qualified name.
    Do so by finding the namespace it belongs to (even if the namespace is being imported).
    Return None if the namespace has not been declared
    """
    db = get_state_engine()

    namespace_id = get_namespace_from_name( name )
    if namespace_id is None or len(namespace_id) == 0:
        return None
    
    namespace = db.get_namespace( namespace_id )
    if namespace is None:
        # maybe importing?
        namespace = db.get_namespace_reveal( namespace_id )
        
    if namespace is None:
        # no such namespace
        return None
    
    name_fee = price_name( get_name_from_fq_name( name ), namespace )
    return name_fee


def get_max_subsidy( testset=False ):
    """
    Get the maximum subsidy we offer, and get a key with a suitable balance
    to pay the subsidy.
    
    Return (subsidy, key)
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    if blockstore_opts.get("max_subsidy") is None:
        return (None, None)
    
    return blockstore_opts["max_subsidy"]
        
        
def make_subsidized_tx( unsigned_tx, fee_cb, max_subsidy, subsidy_key, blockchain_client_inst ):
    """
    Create a subsidized transaction
    transaction and a callback that determines the fee structure.
    """
    
    # subsidize the transaction 
    subsidized_tx = tx_make_subsidizable( unsigned_tx, fee_cb, max_subsidy, subsidy_key, blockchain_client_inst )
    if subsidized_tx is None:
        return {"error": "Order exceeds maximum subsidy"}
    
    else:
        resp = {
            "subsidized_tx": subsidized_tx
        }
        return resp

    
def broadcast_subsidized_tx( subsidized_tx ):
    """
    Broadcast a subsidized tx to the blockchain.
    """
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    # broadcast 
    response = pybitcoin.broadcast_transaction( subsidized_tx, broadcaster_client_inst, format='hex' )
    return response


def blockstore_name_preorder( name, privatekey, register_addr, tx_only=False, pay_fee=True, subsidy_key=None, public_key=None, testset=False, consensus_hash=None ):
    """
    Preorder a name.
    
    @name: the name to preorder 
    @register_addr: the address that will own the name upon registration 
    @privatekey: the private key that will pay for the preorder. Can be None if we're subsidizing (in which case subsidy_key and public_key are required)
    @tx_only: if True, then return only the unsigned serialized transaction.  Do not broadcast it.
    @pay_fee: if False, then return a subsidized serialized transaction, where we have signed our 
    inputs/outputs with SIGHASH_ANYONECANPAY.  The caller will need to sign their input and then 
    broadcast it.
    
    Return a JSON object on success.
    Return a JSON object with 'error' set on error.
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    db = get_state_engine()

    if consensus_hash is None:
        consensus_hash = db.get_current_consensus()
    
    if consensus_hash is None:
        # consensus hash must exist
        return {"error": "Nameset snapshot not found."}
        
    if db.is_name_registered( name ):
        # name can't be registered
        return {"error": "Name already registered"}

    namespace_id = get_namespace_from_name( name )
    
    if not db.is_namespace_ready( namespace_id ):
        # namespace must be ready; otherwise this is a waste
        return {"error": "Namespace is not ready"}
    
    name_fee = get_name_cost( name )
        
    log.debug("The price of '%s' is %s satoshis" % (name, name_fee))
    
    try:
        resp = preorder_name(str(name), privatekey, str(register_addr), str(consensus_hash), blockchain_client_inst, \
            name_fee, testset=blockstore_opts['testset'], pay_fee=pay_fee, public_key=public_key, tx_only=tx_only)
    except:
        return json_traceback()

    if tx_only and not pay_fee and subsidy_key is not None:
        # subsidize the transaction 
        resp = make_subsidized_tx( resp['unsigned_tx'], preorder_fees, blockstore_opts['max_subsidy'], subsidy_key, blockchain_client_inst )
        
    log.debug('preorder <%s, %s, %s>' % (name, privatekey, consensus_hash))

    return resp


def blockstore_name_register( name, privatekey, register_addr, renewal_fee=None, tx_only=False, pay_fee=True, subsidy_key=None, public_key=None, testset=False, consensus_hash=None ):
    """
    Register or renew a name
    
    @name: the name to register 
    @register_addr: the address that will own the name (must be the same as the address 
    given on preorder)
    @privatekey: if registering, this is the key that will pay for the registration (must 
    be the same key as the key used to preorder).  If renewing, this is the private key of the 
    name owner's address.
    @renewal_fee: if given, this is the fee to renew the name (must be at least the 
    cost of the name itself)
    @tx_only: if True, then return only the unsigned serialized transaction. Do not broadcast it.
    @pay_fee: if False, then do not pay any associated dust or operational fees.  This should be used 
    to generate a signed serialized transaction that another key will later subsidize
    
    Return a JSON object on success 
    Return a JSON object with 'error' set on error.
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    db = get_state_engine()
    
    if db.is_name_registered( name ) and renewal_fee is None:
        # *must* be given, so we don't accidentally charge
        return {"error": "Name already registered"}
    
    try:
        resp = register_name(str(name), privatekey, str(register_addr), blockchain_client_inst, renewal_fee=renewal_fee, \
            tx_only=tx_only, pay_fee=pay_fee, blockchain_broadcaster=broadcaster_client_inst, testset=blockstore_opts['testset'], \
            public_key=public_key )
    except:
        return json_traceback()
    
    if tx_only and not pay_fee and subsidy_key is not None:
        # subsidize the transaction 
        resp = make_subsidized_tx( resp['unsigned_tx'], registration_fees, blockstore_opts['max_subsidy'], subsidy_key, blockchain_client_inst )
        
    log.debug("name register/renew: %s" % name)
    return resp


def blockstore_name_update( name, data_hash, privatekey, tx_only=False, pay_fee=True, public_key=None, subsidy_key=None, testset=False, consensus_hash=None ):
    """
    Update a name with new data.
    
    @name: the name to update
    @data_hash: the hash of the new name record 
    @privatekey: the private key of the owning address.
    @tx_only: if True, then return only the unsigned serialized transaction.  Do not broadcast it.
    @pay_fee: if False, then do not pay any associated dust or operational fees.  This should be 
    used to generate a signed serialized transaction that another key will later subsidize.
    
    Return a JSON object on success 
    Return a JSON object with 'error' set on error.
    """
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    db = get_state_engine()
    
    if consensus_hash is None:
        consensus_hash = db.get_current_consensus()

    if consensus_hash is None:
        return {"error": "Nameset snapshot not found."}

    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}

    try:
        resp = update_name(str(name), str(data_hash), str(consensus_hash), privatekey, blockchain_client_inst, \
            tx_only=tx_only, pay_fee=pay_fee, blockchain_broadcaster=broadcaster_client_inst, public_key=public_key, testset=blockstore_opts['testset'])
    except:
        return json_traceback()

    if tx_only and not pay_fee and subsidy_key is not None:
        # subsidize the transaction 
        resp = make_subsidized_tx( resp['unsigned_tx'], update_fees, blockstore_opts['max_subsidy'], subsidy_key, blockchain_client_inst )
    
    log.debug('name update <%s, %s, %s, %s>' % (name, data_hash, privatekey, consensus_hash))
    return resp


def blockstore_name_transfer( name, address, keepdata, privatekey, public_key=None, subsidy_key=None, tx_only=False, pay_fee=True, testset=False, consensus_hash=None ):
    """
    Transfer a name to a new address.
    
    @name: the name to transfer
    @address:  the new address to own the name 
    @keepdata: if True, then keep the name record tied to the name.  Otherwise, discard it.
    @privatekey: the private key of the owning address.
    @tx_only: if True, then return only the unsigned serialized transaction.  Do not broadcast it.
    @pay_fee: if False, then do not pay any associated dust or operational fees.  This should be 
    used to generate a signed serialized transaction that another key will later subsidize.
    
    Return a JSON object on success 
    Return a JSON object with 'error' set on error.
    """
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    db = get_state_engine()
    
    if consensus_hash is None:
        consensus_hash = db.get_current_consensus()

    if consensus_hash is None:
        return {"error": "Nameset snapshot not found."}
    
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    try:
        resp = transfer_name(str(name), str(address), bool(keepdata), str(consensus_hash), privatekey, blockchain_client_inst, \
            tx_only=tx_only, pay_fee=pay_fee, blockchain_broadcaster=broadcaster_client_inst, public_key=public_key, testset=blockstore_opts['testset'])
    except:
        return json_traceback()

    if tx_only and not pay_fee and subsidy_key is not None:
        # subsidize the transaction 
        resp = make_subsidized_tx( resp['unsigned_tx'], transfer_fees, blockstore_opts['max_subsidy'], subsidy_key, blockchain_client_inst )
 
    log.debug('name transfer <%s, %s, %s>' % (name, address, privatekey))

    return resp


def blockstore_name_renew( name, privatekey, tx_only=False, pay_fee=True, subsidy_key=None, public_key=None, testset=False, consensus_hash=None ):
    """
    Renew a name
    
    @name: the name to renew 
    @privatekey: the private key of the name owner
    @tx_only: if True, then return only the unsigned serialized transaction.  Do not broadcast it.
    @pay_fee: if False, then do not pay any associated dust or operational fees.  This should be 
    used to generate a signed serialized transaction that another key will later subsidize.
    
    Return a JSON object on success 
    Return a JSON object with 'error' set on error.
    """
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    # renew the name for the caller
    db = get_state_engine()
    name_rec = db.get_name( name )
    if name_rec is None:
        return {"error": "Name is not registered"}
    
    # renew to the caller
    register_addr = name_rec['address']
    renewal_fee = get_name_cost( name )
    
    return blockstore_name_register( name, privatekey, register_addr, renewal_fee=renewal_fee, tx_only=tx_only, pay_fee=pay_fee, subsidy_key=subsidy_key, public_key=public_key, testset=testset )


def blockstore_name_revoke( name, privatekey, tx_only=False, pay_fee=True, subsidy_key=None, public_key=None, testset=False, consensus_hash=None ):
    """
    Revoke a name and all its data.
    
    @name: the name to renew 
    @privatekey: the private key of the name owner
    @tx_only: if True, then return only the unsigned serialized transaction.  Do not broadcast it.
    @pay_fee: if False, then do not pay any associated dust or operational fees.  This should be 
    used to generate a signed serialized transaction that another key will later subsidize.
    
    Return a JSON object on success 
    Return a JSON object with 'error' set on error.
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    try:
        resp = revoke_name(str(name), privatekey, blockchain_client_inst, \
            tx_only=tx_only, blockchain_broadcaster=broadcaster_client_inst, public_key=public_key, testset=blockstore_opts['testset'])
    except:
        return json_traceback()
    
    if tx_only and not pay_fee and subsidy_key is not None:
        # subsidize the transaction 
        resp = make_subsidized_tx( resp['unsigned_tx'], revoke_fees, blockstore_opts['max_subsidy'], subsidy_key, blockchain_client_inst )

    log.debug("name revoke <%s>" % name )
    
    return resp


def blockstore_name_import( name, recipient_address, update_hash, privatekey, tx_only=False, testset=False, consensus_hash=None ):
    """
    Import a name into a namespace.
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    db = get_state_engine()
    
    try:
        resp = name_import( str(name), str(recipient_address), str(update_hash), str(privatekey), blockchain_client_inst, \
            tx_only=tx_only, blockchain_broadcaster=broadcaster_client_inst, testset=blockstore_opts['testset'] )
    except:
        return json_traceback()
    
    log.debug("import <%s>" % name )
    
    return resp
    

def blockstore_namespace_preorder( namespace_id, register_addr, privatekey, tx_only=False, testset=False, consensus_hash=None ):
    """
    Define the properties of a namespace.
    Between the namespace definition and the "namespace begin" operation, only the 
    user who created the namespace can create names in it.
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    db = get_state_engine()
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    if consensus_hash is None:
        consensus_hash = db.get_current_consensus()
    
    if consensus_hash is None:
        return {"error": "Nameset snapshot not found."}

    namespace_fee = price_namespace( namespace_id )
    
    log.debug("Namespace '%s' will cost %s satoshis" % (namespace_id, namespace_fee))
    
    try:
        resp = namespace_preorder( str(namespace_id), str(register_addr), str(consensus_hash), str(privatekey), blockchain_client_inst, namespace_fee, \
            tx_only=tx_only, blockchain_broadcaster=broadcaster_client_inst, testset=blockstore_opts['testset'] )
        
    except:
        return json_traceback()
    
    log.debug("namespace_preorder <%s>" % (namespace_id))
    return resp 


def blockstore_namespace_reveal( namespace_id, register_addr, lifetime, coeff, base, bucket_exponents, nonalpha_discount, no_vowel_discount, privatekey, tx_only=False, testset=False, consensus_hash=None ):
    """
    Reveal and define the properties of a namespace.
    Between the namespace definition and the "namespace begin" operation, only the 
    user who created the namespace can create names in it.
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    try:
        resp = namespace_reveal( str(namespace_id), str(register_addr), int(lifetime), \
                                int(coeff), int(base), list(bucket_exponents), \
                                int(nonalpha_discount), int(no_vowel_discount), \
                                str(privatekey), blockchain_client_inst, \
                                blockchain_broadcaster=broadcaster_client_inst, testset=blockstore_opts['testset'], tx_only=tx_only )
    except:
        return json_traceback()
    
    log.debug("namespace_reveal <%s, %s, %s, %s, %s, %s, %s>" % (namespace_id, lifetime, coeff, base, bucket_exponents, nonalpha_discount, no_vowel_discount))
    return resp 
    
    
def blockstore_namespace_ready( namespace_id, privatekey, tx_only=False, testset=False, consensus_hash=None ):
    """
    Declare that a namespace is open to accepting new names.
    """
    
    blockstore_opts = default_blockstore_opts( virtualchain.get_config_filename(), testset=testset )
    
    # are we doing our initial indexing?
    if is_indexing():
        return {"error": "Indexing blockchain"}
    
    blockchain_client_inst = get_utxo_provider_client()
    if blockchain_client_inst is None:
        return {"error": "Failed to connect to blockchain UTXO provider"}
    
    broadcaster_client_inst = get_tx_broadcaster()
    if broadcaster_client_inst is None:
        return {"error": "Failed to connect to blockchain transaction broadcaster"}
    
    try:
        resp = namespace_ready( str(namespace_id), str(privatekey), blockchain_client_inst, \
            tx_only=tx_only, blockchain_broadcaster=broadcaster_client_inst, testset=blockstore_opts['testset'] )
    except:
        return json_traceback()
    
    log.debug("namespace_ready %s" % namespace_id )
    return resp


class BlockstoredRPC(jsonrpc.JSONRPC, object):
    """
    Blockstored not-quote-JSON-RPC server.
    
    We say "not quite" because the implementation serves data 
    via Netstrings, not HTTP, and does not pay attention to 
    the 'id' or 'version' fields in the JSONRPC spec.
    
    This endpoint does *not* talk to a storage provider, but only 
    serves back information from the blockstore virtual chain.
    
    The client is responsible for resolving this information 
    to data, via an ancillary storage provider.
    """
    
    def __init__(self, testset=False ):
        self.testset = testset 
        super( BlockstoredRPC, self ).__init__()
    
    def jsonrpc_ping(self):
        reply = {}
        reply['status'] = "alive"
        return reply


    def jsonrpc_get_name_blockchain_record(self, name):
        """
        Lookup the blockchain-derived profile for a name.
        """
        
        blockstore_state_engine = get_state_engine()
        name_record = blockstore_state_engine.get_name( name )
        
        if name_record is None:
           if is_indexing():
               return {"error": "Indexing blockchain"}
           else:
               return {"error": "Not found."}
        
        else:
           return name_record 
      

    def jsonrpc_get_name_blockchain_history( self, name, start_block, end_block ):
        """
        Get the sequence of name operations processed for a given name.
        """
        blockstore_state_engine = get_state_engine()
        name_history = blockstore_state_engine.get_name_history( name, start_block, end_block )
        
        if name_history is None:
            if is_indexing():
                return {"error": "Indexing blockchain"}
            else:
                return {"error": "Not found."}

        else:
            return name_history
        

    def jsonrpc_getinfo(self):
        """
        Get the number of blocks the
        """
        bitcoind_opts = default_bitcoind_opts( virtualchain.get_config_filename() )
        bitcoind = get_bitcoind( new_bitcoind_opts=bitcoind_opts, new=True )
        
        info = bitcoind.getinfo()
        reply = {}
        reply['bitcoind_blocks'] = info['blocks']
        
        db = get_state_engine()
        reply['consensus'] = db.get_current_consensus()
        reply['blocks'] = db.get_current_block()
        return reply


    def jsonrpc_preorder( self, name, privatekey, register_addr ):
        """
        Preorder a name:
        @name is the name to preorder 
        @register_addr is the address of the key pair that will own the name 
        @privatekey is the private key that will send the preorder transaction
        (it must be *different* from the register_addr keypair)
        
        Returns a JSON object with the transaction ID on success.
        Returns a JSON object with 'error' on error.
        """
        return blockstore_name_preorder( str(name), str(privatekey), str(register_addr), testset=self.testset )
    
    
    def jsonrpc_preorder_tx( self, name, privatekey, register_addr ):
        """
        Generate a transaction that preorders a name:
        @name is the name to preorder 
        @register_addr is the address of the key pair that will own the name 
        @privatekey is the private key that will send the preorder transaction
        (it must be *different* from the register_addr keypair)
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_preorder( str(name), str(privatekey), str(register_addr), tx_only=True, testset=self.testset )
    
    
    def jsonrpc_preorder_tx_subsidized( self, name, public_key, register_addr, subsidy_key ):
        """
        Generate a transaction that preorders a name, but without paying fees.
        @name is the name to preorder 
        @register_addr is the address of the key pair that will own the name 
        public_key is the client's public key that will sign the preorder transaction
        (it must be *different* from the register_addr keypair)
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_preorder( str(name), None, str(register_addr), tx_only=True, pay_fee=False, subsidy_key=str(subsidy_key), public_key=str(public_key), testset=self.testset )
    
    
    def jsonrpc_register( self, name, privatekey, register_addr ):
        """
        Register a name:
        @name is the name to register 
        @register_addr is the address of the key pair that will own the name 
        (given earlier in the preorder)
        @privatekey is the private key that sent the preorder transaction.
        
        Returns a JSON object with the transaction ID on success.
        Returns a JSON object with 'error' on error.
        """
        return blockstore_name_register( str(name), str(privatekey), str(register_addr), testset=self.testset )
    
    
    def jsonrpc_register_tx( self, name, privatekey, register_addr ):
        """
        Generate a transaction that will register a name:
        @name is the name to register 
        @register_addr is the address of the key pair that will own the name 
        (given earlier in the preorder)
        @privatekey is the private key that sent the preorder transaction.
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_register( str(name), str(privatekey), str(register_addr), tx_only=True, testset=self.testset )
    
    
    def jsonrpc_register_tx_subsidized( self, name, public_key, register_addr, subsidy_key ):
        """
        Generate a subsidizable transaction that will register a name
        @name is the name to register 
        @register_addr is the address of the key pair that will own the name 
        (given earlier in the preorder)
        public_key is the public key whose private counterpart sent the preorder transaction.
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_register( str(name), None, str(register_addr), tx_only=True, pay_fee=False, public_key=str(public_key), subsidy_key=str(subsidy_key), testset=self.testset )
    
    
    def jsonrpc_update( self, name, data_hash, privatekey ):
        """
        Update a name's record:
        @name is the name to update 
        @data_hash is the hash of the new name record
        @privatekey is the private key that owns the name
        
        Returns a JSON object with the transaction ID on success.
        Returns a JSON object with 'error' on error.
        """
        return blockstore_name_update( str(name), str(data_hash), str(privatekey), testset=self.testset )
    
    
    def jsonrpc_update_tx( self, name, data_hash, privatekey ):
        """
        Generate a transaction that will update a name's name record hash.
        @name is the name to update 
        @data_hash is the hash of the new name record
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_update( str(name), str(data_hash), str(privatekey), tx_only=True, testset=self.testset )
    
    
    def jsonrpc_update_tx_subsidized( self, name, data_hash, public_key, subsidy_key ):
        """
        Generate a subsidizable transaction that will update a name's name record hash.
        @name is the name to update 
        @data_hash is the hash of the new name record
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_update( str(name), str(data_hash), None, public_key=str(public_key), subsidy_key=str(subsidy_key), tx_only=True, pay_fee=False, testset=self.testset )
    
    
    def jsonrpc_transfer( self, name, address, keepdata, privatekey ):
        """
        Transfer a name's record to a new address
        @name is the name to transfer 
        @address is the new address that will own the name 
        @keepdata determines whether or not the name record will
        remain associated with the name on transfer.
        @privatekey is the private key that owns the name
        
        Returns a JSON object with the transaction ID on success.
        Returns a JSON object with 'error' on error.
        """
        return blockstore_name_transfer( str(name), str(address), bool(keepdata), str(privatekey), testset=self.testset )
    
    
    def jsonrpc_transfer_tx( self, name, address, keepdata, privatekey ):
        """
        Generate a transaction that will transfer a name to a new address
        @name is the name to transfer 
        @address is the new address that will own the name 
        @keepdata determines whether or not the name record will
        remain associated with the name on transfer.
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_transfer( str(name), str(address), bool(keepdata), str(privatekey), tx_only=True, testset=self.testset )
    
    
    def jsonrpc_transfer_tx_subsidized( self, name, address, keepdata, public_key, subsidy_key ):
        """
        Generate a subsidizable transaction that will transfer a name to a new address
        @name is the name to transfer 
        @address is the new address that will own the name 
        @keepdata determines whether or not the name record will
        remain associated with the name on transfer.
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_transfer( str(name), str(address), bool(keepdata), None, public_key=str(public_key), subsidy_key=str(subsidy_key), tx_only=True, pay_fee=False, testset=self.testset )
    
   
    def jsonrpc_renew( self, name, privatekey ):
        """
        Renew a name:
        @name is the name to renew 
        @privatekey is the private key that owns the name
        
        Returns a JSON object with the transaction ID on success.
        Returns a JSON object with 'error' on error.
        """
        return blockstore_name_renew( str(name), str(privatekey), testset=self.testset )
    
    
    def jsonrpc_renew_tx( self, name, privatekey ):
        """
        Generate a transaction that will register a name:
        @name is the name to renew 
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_renew( str(name), str(privatekey), tx_only=True, testset=self.testset )
    
    
    def jsonrpc_renew_tx_subsidized( self, name, public_key, subsidy_key ):
        """
        Generate a subsidizable transaction that will register a name
        @name is the name to renew 
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_renew( name, None, public_key=str(public_key), subsidy_key=str(subsidy_key), tx_only=True, pay_fee=False, testset=self.testset )
    
    
    def jsonrpc_revoke( self, name, privatekey ):
        """
        revoke a name:
        @name is the name to revoke 
        @privatekey is the private key that owns the name
        
        Returns a JSON object with the transaction ID on success.
        Returns a JSON object with 'error' on error.
        """
        return blockstore_name_revoke( str(name), str(privatekey), testset=self.testset )
    
    
    def jsonrpc_revoke_tx( self, name, privatekey ):
        """
        Generate a transaction that will revoke a name:
        @name is the name to revoke 
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_revoke( str(name), str(privatekey), tx_only=True, testset=self.testset )
    
    
    def jsonrpc_revoke_tx_subsidized( self, name, public_key, subsidy_key ):
        """
        Generate a subsidizable transaction that will revoke a name
        @name is the name to revoke 
        @privatekey is the private key that owns the name
        
        Return a JSON object with the signed serialized transaction on success.  It will not be broadcast.
        Return a JSON object with 'error' on error.
        """
        return blockstore_name_revoke( str(name), None, public_key=str(public_key), subsidy_key=str(subsidy_key), tx_only=True, pay_fee=False, testset=self.testset )
    
    
    def jsonrpc_name_import( self, name, recipient_address, update_hash, privatekey ):
        """
        Import a name into a namespace.
        """
        return blockstore_name_import( name, recipient_address, update_hash, privatekey, testset=self.testset )
    
    
    def jsonrpc_name_import_tx( self, name, recipient_address, update_hash, privatekey ):
        """
        Generate a tx that will import a name 
        """
        return blockstore_name_import( name, recipient_address, update_hash, privatekey, tx_only=True, testset=self.testset )
        
    
    def jsonrpc_namespace_preorder( self, namespace_id, reveal_addr, privatekey ):
        """
        Define the properties of a namespace.
        Between the namespace definition and the "namespace begin" operation, only the 
        user who created the namespace can create names in it.
        """
        return blockstore_namespace_preorder( namespace_id, reveal_addr, privatekey, testset=self.testset )
    
    
    def jsonrpc_namespace_preorder_tx( self, namespace_id, reveal_addr, privatekey ):
        """
        Create a signed transaction that will define the properties of a namespace.
        Between the namespace definition and the "namespace begin" operation, only the 
        user who created the namespace can create names in it.
        """
        return blockstore_namespace_preorder( namespace_id, reveal_addr, privatekey, tx_only=True, testset=self.testset )
    
    
    def jsonrpc_namespace_reveal( self, namespace_id, reveal_addr, lifetime, coeff, base, bucket_exponents, nonalpha_discount, no_vowel_discount, privatekey ):
        """
        Reveal and define the properties of a namespace.
        Between the namespace definition and the "namespace begin" operation, only the 
        user who created the namespace can create names in it.
        """
        return blockstore_namespace_reveal( namespace_id, reveal_addr, lifetime, coeff, base, bucket_exponents, nonalpha_discount, no_vowel_discount, privatekey, testset=self.testset )
     
     
    def jsonrpc_namespace_reveal_tx( self, namespace_id, reveal_addr, lifetime, coeff, base, bucket_exponents, nonalpha_discount, no_vowel_discount, privatekey ):
        """
        Generate a signed transaction that will reveal and define the properties of a namespace.
        Between the namespace definition and the "namespace begin" operation, only the 
        user who created the namespace can create names in it.
        """
        return blockstore_namespace_reveal( namespace_id, reveal_addr, lifetime, coeff, base, bucket_exponents, nonalpha_discount, no_vowel_discount, privatekey, tx_only=True, testset=self.testset )
     
     
    def jsonrpc_namespace_ready( self, namespace_id, privatekey ):
        """
        Declare that a namespace is open to accepting new names.
        """
        return blockstore_namespace_ready( namespace_id, privatekey, testset=self.testset )
    
    
    def jsonrpc_namespace_ready_tx( self, namespace_id, privatekey ):
        """
        Create a signed transaction that will declare that a namespace is open to accepting new names.
        """
        return blockstore_namespace_ready( namespace_id, privatekey, tx_only=True, testset=self.testset )
    
    
    def jsonrpc_get_name_cost( self, name ):
        """
        Return the cost of a given name, including fees
        Return value is in satoshis
        """
        
        # are we doing our initial indexing?
        
        if len(name) > LENGTHS['blockchain_id_name']:
            return {"error": "Name too long"}
        
        ret = get_name_cost( name )
        if ret is None:
            if is_indexing():
               return {"error": "Indexing blockchain"}
            
            else:
               return {"error": "Unknown/invalid namespace"}
        
        return {"satoshis": int(math.ceil(ret))}
        
        
    def jsonrpc_get_namespace_cost( self, namespace_id ):
        """
        Return the cost of a given namespace, including fees.
        Return value is in satoshis
        """
        
        if len(namespace_id) > LENGTHS['blockchain_id_namespace_id']:
            return {"error": "Namespace ID too long"}
        
        ret = price_namespace(namespace_id)
        return {"satoshis": int(math.ceil(ret))}
        
        
    def jsonrpc_get_namespace_blockchain_record( self, namespace_id ):
        """
        Return the readied namespace with the given namespace_id
        """
        
        db = get_state_engine()
        ns = db.get_namespace( namespace_id )
        if ns is None:
            if is_indexing():
                return {"error": "Indexing blockchain"}
            else:
                return {"error": "No such ready namespace"}
        else:
            return ns
        
    
    def jsonrpc_get_all_names( self, offset, count ):
        """
        Return all names 
        """
        # are we doing our initial indexing?
        if is_indexing():
            return {"error": "Indexing blockchain"}
        
        db = get_state_engine()
        return db.get_all_names( offset=offset, count=count )
    
    
    def jsonrpc_get_names_in_namespace( self, namespace_id, offset, count ):
        """
        Return all names in a namespace
        """
        # are we doing our initial indexing?
        if is_indexing():
            return {"error": "Indexing blockchain"}
        
        db = get_state_engine()
        return db.get_names_in_namespace( namespace_id, offset=offset, count=count )
    
    
    def jsonrpc_get_consensus_at( self, block_id ):
        """
        Return the consensus hash at a block number 
        """
        db = get_state_engine()
        return db.get_consensus_at( block_id )
    
            
        
def run_indexer( testset=False ):
    """
    Continuously reindex the blockchain, but as a subprocess.
    """
    
    # set up this process
    signal.signal( signal.SIGINT, die_handler_indexer )
    signal.signal( signal.SIGQUIT, die_handler_indexer )
    signal.signal( signal.SIGTERM, die_handler_indexer )

    bitcoind_opts = get_bitcoin_opts()
    
    _, last_block_id = get_index_range()
    blockstore_state_engine = get_state_engine()
    
    while True:
        
        time.sleep( REINDEX_FREQUENCY )
        virtualchain.sync_virtualchain( bitcoind_opts, last_block_id, blockstore_state_engine )
        
        _, last_block_id = get_index_range()
        
    return


def stop_server():
    """
    Stop the blockstored server.
    """
    global indexer_pid 
    
    # Quick hack to kill a background daemon
    pid_file = get_pidfile_path()

    try:
        fin = open(pid_file, "r")
    except Exception, e:
        return
        
    else:
        pid_data = fin.read()
        fin.close()
        os.remove(pid_file)

        pid = int(pid_data)
        
        try:
           os.kill(pid, signal.SIGKILL)
        except Exception, e:
           return 

    
    if indexer_pid is not None:
        try:
           os.kill(indexer_pid, signal.SIGTERM)
        except Exception, e:
           return 
       
    # stop building new state if we're in the middle of it
    db = get_state_engine()
    virtualchain.stop_sync_virtualchain( db )
    
    set_indexing( False )
    
       
def get_indexing_lockfile():
    """
    Return path to the indexing lockfile 
    """
    return os.path.join( virtualchain.get_working_dir(), "blockstore.indexing" )
       
       
def is_indexing():
    """
    Is the blockstore daemon synchronizing with the blockchain?
    """
    indexing_path = get_indexing_lockfile()
    if os.path.exists( indexing_path ):
        return True 
    else:
        return False
        
        
def set_indexing( flag ):
    """
    Set a flag in the filesystem as to whether or not we're indexing.
    """
    indexing_path = get_indexing_lockfile()
    if flag:
        try:
            fd = open( indexing_path, "w+" )
            fd.close()
            return True
        except:
            return False 
        
    else:
        try:
            os.unlink( indexing_path )
            return True
        except:
            return False 
        

def run_server( testset=False, foreground=False ):
    """ 
    Run the blockstored RPC server, optionally in the foreground.
    """
    
    global indexer_pid
    
    bt_opts = get_bitcoin_opts()
    
    tac_file = get_tacfile_path( testset=testset )
    access_log_file = get_logfile_path() + ".access"
    indexer_log_file = get_logfile_path() + ".indexer"
    pid_file = get_pidfile_path()
    
    start_block, current_block = get_index_range()
    
    argv0 = os.path.normpath( sys.argv[0] )
    
    if os.path.exists("./%s" % argv0 ):
        if testset:
            indexer_command = ("%s indexer --testset" % (os.path.join( os.getcwd(), argv0))).split()
        else:
            indexer_command = ("%s indexer" % (os.path.join( os.getcwd(), argv0))).split()
    else:
        # hope its in the $PATH
        if testset:
            indexer_command = ("%s indexer --testset" % argv0).split()
        else:
            indexer_command = ("%s indexer" % argv0).split()
    
    logfile = None
    if not foreground:

        api_server_command = ('twistd --pidfile=%s --logfile=%s -noy %s' % (pid_file,
                                                                            access_log_file,
                                                                            tac_file)).split()

        try:
            if os.path.exists( indexer_log_file ):
                logfile = open( indexer_log_file, "a" )
            else:
                logfile = open( indexer_log_file, "a+" )
        except OSError, oe:
            log.error("Failed to open '%s': %s" % (indexer_log_file, oe.strerror))
            sys.exit(1)
        
        # become a daemon 
        child_pid = os.fork()
        if child_pid == 0:
            
            # child! detach, setsid, and make a new child to be adopted by init 
            sys.stdin.close()
            os.dup2( logfile.fileno(), sys.stdout.fileno() )
            os.dup2( logfile.fileno(), sys.stderr.fileno() )
            os.setsid()
            
            daemon_pid = os.fork()
            if daemon_pid == 0:
                
                # daemon!
                os.chdir("/")
            
            elif daemon_pid > 0:
                
                # parent!
                sys.exit(0)
                
            else:
                
                # error
                sys.exit(1)
                
        elif child_pid > 0:
            
            # parent
            # wait for child 
            pid, status = os.waitpid( child_pid, 0 )
            sys.exit(status)
            
    else:
        
        # foreground
        api_server_command = ('twistd --pidfile=%s -noy %s' % (pid_file, tac_file)).split()
        
    
    # start API server
    blockstored = subprocess.Popen( api_server_command, shell=False)    
    
    set_indexing( False )
    
    if start_block != current_block:
        # bring us up to speed 
        set_indexing( True )
    
        blockstore_state_engine = get_state_engine()
        virtualchain.sync_virtualchain( bt_opts, current_block, blockstore_state_engine )
        
        set_indexing( False )
    
    # fork the indexer 
    if foreground:
        indexer = subprocess.Popen( indexer_command, shell=False )
    else:
        indexer = subprocess.Popen( indexer_command, shell=False, stdout=logfile, stderr=logfile )
        
    indexer_pid = indexer.pid
    
    # wait for the API server to die (we kill it with `blockstored stop`)
    blockstored.wait()
    
    # stop our indexer subprocess 
    indexer_pid = None
    
    os.kill( indexer.pid, signal.SIGINT )
    indexer.wait()
   
    if logfile is not None:
        logfile.flush()
        logfile.close()
    
    # stop building new state if we're in the middle of it
    db = get_state_engine()
    virtualchain.stop_sync_virtualchain( db )
    
    return blockstored.returncode 


def setup( testset=False, return_parser=False ):
   """
   Do one-time initialization.
   Call this to set up global state and set signal handlers.
   
   If return_parser is True, return a partially-
   setup argument parser to be populated with 
   subparsers (i.e. as part of main())
   
   Otherwise return None.
   """
   
   global blockstore_opts 
   global blockchain_client
   global blockchain_broadcaster
   global bitcoin_opts
   global utxo_opts 
   global dht_opts
   
   # set up our implementation 
   virtualchain.setup_virtualchain( blockstore_state_engine, testset=testset )
   
   # acquire configuration, and store it globally
   blockstore_opts, bitcoin_opts, utxo_opts, dht_opts = configure( interactive=True, testset=testset )
   
   # ensure testset intent
   if blockstore_opts['testset'] != testset:
       raise Exception("Blockstore configured to run with testset=%s, but given testset=%s via command-line" % (blockstore_opts['testset'], testset))
   
   # merge in command-line bitcoind options 
   config_file = virtualchain.get_config_filename()
   
   arg_bitcoin_opts = None 
   argparser = None 
   
   if return_parser:
      arg_bitcoin_opts, argparser = virtualchain.parse_bitcoind_args( return_parser=return_parser )
   
   else:
      arg_bitcoin_opts = virtualchain.parse_bitcoind_args( return_parser=return_parser )

   # command-line overrides config file
   for (k, v) in arg_bitcoin_opts.items():
      bitcoin_opts[k] = v
   
   # store options 
   set_bitcoin_opts( bitcoin_opts )
   set_utxo_opts( utxo_opts )
   
   if return_parser:
      return argparser 
   else:
      return None
  
  
def reconfigure( testset=False ):
   """
   Reconfigure blockstored.
   """
   configure( force=True, testset=testset )
   print "Blockstore successfully reconfigured."
   sys.exit(0)


def clean( testset=False, confirm=True ):
    """
    Remove blockstore's db, lastblock, and snapshot files.
    Prompt for confirmation 
    """
   
    delete = False 
    exit_status = 0
    
    if confirm:
        warning = "WARNING: THIS WILL DELETE YOUR BLOCKSTORE DATABASE!\n"
        warning+= "Are you sure you want to proceed?\n"
        warning+= "Type 'YES' if so: "
        value = raw_input( warning )
        
        if value != "YES":
            sys.exit(exit_status)
        
        else:
            delete = True 
       
    else:
        delete = True 
        
    
    if delete:
        print "Deleting..."
       
        db_filename = virtualchain.get_db_filename()
        lastblock_filename = virtualchain.get_lastblock_filename()
        snapshots_filename = virtualchain.get_snapshots_filename()
        
        for path in [db_filename, lastblock_filename, snapshots_filename]:
            try:
                os.unlink( path )
            except:
                log.warning("Unable to delete '%s'" % path)
                exit_status = 1
                
    sys.exit(exit_status)


def rec_to_virtualchain_op( name_rec, block_number, untrusted_db, testset=False ):
    """
    Given a record from the blockstore database,
    convert it into a virtualchain operation to 
    process.
    """

    # apply opcodes so we can consume them with virtualchain
    opcode_name = str(name_rec['opcode'])
    ret_op = {}

    if name_rec.has_key('expired') and name_rec['expired']:
        # don't care
        return None

    if opcode_name == "NAME_PREORDER":
        name_rec_script = build_preorder( None, None, None, str(name_rec['consensus_hash']), name_hash=str(name_rec['preorder_name_hash']), testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_preorder( name_rec_payload ) 

    elif opcode_name == "NAME_REGISTRATION":
        name_rec_script = build_registration( str(name_rec['name']), testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_registration( name_rec_payload )

        # reconstruct the registration op...
        ret_op['recipient'] = str(name_rec['sender'])
        ret_op['recipient_address'] = str(name_rec['address'])
        ret_op['sender'] = pybitcoin.make_pay_to_address_script( pybitcoin.BitcoinPublicKey( str(name_rec['sender_pubkey']) ).address() )

    elif opcode_name == "NAME_UPDATE":
        data_hash = None
        if name_rec['value_hash'] is not None:
            data_hash = str(name_rec['value_hash'])

        name_rec_script = build_update( str(name_rec['name']), str(name_rec['consensus_hash']), data_hash=data_hash, testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_update(name_rec_payload)

    elif opcode_name == "NAME_TRANSFER": 

        # reconstruct the transport op...
        if name_rec['value_hash'] is None:
            name_rec['keep_data'] = False 
        else:
            name_rec['keep_data'] = True 

        # what was the previous owner?
        recipient = name_rec['sender']
        recipient_address = name_rec['address']

        # restore history
        untrusted_name_rec = untrusted_db.get_name( name_rec['name'] )
        name_rec['history'] = untrusted_name_rec['history']

        # get previous owner
        name_rec_prev = BlockstoreDB.restore_from_history( name_rec, block_number - 1 )
        sender = name_rec_prev['sender']
        address = name_rec_prev['address']

        # reconstruct recipient and sender
        name_rec['recipient'] = recipient
        name_rec['recipient_address'] = recipient_address 

        name_rec['sender'] = sender
        name_rec['address'] = address

        name_rec_script = build_transfer( str(name_rec['name']), name_rec['keep_data'], str(name_rec['consensus_hash']), testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_transfer(name_rec_payload, name_rec['recipient'] )

    elif opcode_name == "NAME_REVOKE":
        name_rec_script = build_revoke( str(name_rec['name']), testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_revoke( name_rec_payload )

    elif opcode_name == "NAME_IMPORT":
        name_rec_script = build_name_import( str(name_rec['name']), testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_name_import( name_rec_payload, name_rec['recipient'], name_rec['value_hash'] )
        
        # reconstruct the import op...
        ret_op['recipient'] = str(name_rec['sender'])
        ret_op['recipient_address'] = str(name_rec['address'])
        ret_op['sender'] = pybitcoin.make_pay_to_address_script( pybitcoin.BitcoinPublicKey( str(name_rec['sender_pubkey']) ).address() )

    elif opcode_name == "NAMESPACE_PREORDER":
        name_rec_script = build_namespace_preorder( None, None, None, str(name_rec['consensus_hash']), namespace_id_hash=str(name_rec['namespace_id_hash']), testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_namespace_preorder(name_rec_payload)

    elif opcode_name == "NAMESPACE_REVEAL":
        name_rec_script = build_namespace_reveal( str(name_rec['namespace_id']), name_rec['version'], str(name_rec['recipient_address']), \
                                                  name_rec['lifetime'], name_rec['coeff'], name_rec['base'], name_rec['buckets'],
                                                  name_rec['nonalpha_discount'], name_rec['no_vowel_discount'], testset=testset )

        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_namespace_reveal( name_rec_payload, str(name_rec['sender']), str(name_rec['recipient_address']) )

    elif opcode_name == "NAMESPACE_READY":
        name_rec_script = build_namespace_ready( str(name_rec['namespace_id']), testset=testset )
        name_rec_payload = binascii.unhexlify( name_rec_script )[3:]
        ret_op = parse_namespace_ready( name_rec_payload )

    ret_op = virtualchain.virtualchain_set_opfields( ret_op, virtualchain_opcode=getattr( config, opcode_name ), virtualchain_txid=str(name_rec['txid']) )
    ret_op['opcode'] = opcode_name

    merged_ret_op = copy.deepcopy( name_rec )
    merged_ret_op.update( ret_op )
    return merged_ret_op


def verify_database( trusted_consensus_hash, consensus_block_id, untrusted_db_path, working_db_path=None, start_block=None, testset=False ):
    """
    Verify that a database is consistent with a 
    known-good consensus hash.

    This algorithm works by creating a new database,
    parsing the untrusted database, and feeding the untrusted 
    operations into the new database block-by-block.  If we
    derive the same consensus hash, then we can trust the 
    database.
    """

    if start_block is None:
        start_block = virtualchain.get_first_block_id()

    # reconfigure the virtualchain to use a temporary directory,
    # so we don't interfere with this instance's primary database
    working_dir = tempfile.mkdtemp( prefix='blockstore-verify-database-' )
    blockstore_state_engine.working_dir = working_dir

    virtualchain.setup_virtualchain( blockstore_state_engine, testset=testset )

    # feed in operations, block by block, from the untrusted database
    untrusted_db = BlockstoreDB( untrusted_db_path )

    # working db, to build up the operations in the untrusted db block-by-block
    working_db = None
    if working_db_path is None:
        working_db_path = virtualchain.get_db_filename()

    working_db = BlockstoreDB( working_db_path )

    # map block ID to consensus hashes
    consensus_hashes = {}
    rc = True

    for block_id in xrange( start_block, consensus_block_id+1 ):

        # all operations at this block
        nameops = untrusted_db.get_all_nameops_at( block_id )
        for i in xrange(0, len(nameops)):

            # only trusted fields
            opcode_name = nameops[i]['opcode']
            consensus_fields = SERIALIZE_FIELDS.get( opcode_name, None )
            if consensus_fields is None:
                raise Exception("BUG: no consensus fields defined for '%s'" % opcode_name )

            # remove virtualchain-related fields--they won't be trusted
            nameops[i] = working_db.sanitize_op( nameops[i] )

            for field in nameops[i].keys():

                # remove untrusted fields, except for 'opcode' (which will be fed into the consensus hash
                # indirectly, once the fields are successfully processed and thus proven consistent with 
                # the fields.)
                if field not in consensus_fields and field not in ['opcode']:
                    log.warning("OP '%s': Removing untrusted field '%s'" % (opcode_name, field))
                    del nameops[i][field]

            try:
                # generate virtualchain op
                nameops[i] = rec_to_virtualchain_op( nameops[i], block_id, untrusted_db )
            except:
                print json.dumps( nameops[i], indent=4 )
                raise

        # filter dead ops 
        nameops = filter( lambda x: x is not None, nameops )

        # feed ops to virtualchain to try to reconstruct the db at this block
        consensus_hash = working_db.process_block( block_id, nameops )
        log.debug("VERIFY CONSENSUS(%s): %s" % (block_id, consensus_hash))

        consensus_hashes[block_id] = consensus_hash 


    # did we reach the consensus hash we expected?
    if rc and consensus_hashes[consensus_block_id] == trusted_consensus_hash:
        # clean up
        shutil.rmtree( working_dir )
        if os.path.exists( working_dir ):
            os.rmdir( working_dir )

        return True 

    else:
        log.error("Unverifiable database state stored in '%s'" % working_dir)
        return False
    

def check_testset_enabled():
    """
    Check sys.argv to see if testset is enabled.
    Must be done before we initialize the virtual chain.
    """
    for arg in sys.argv:
        if arg == "--testset":
            return True 
        
    return False


def run_blockstored():
   """
   run blockstored
   """
   
   testset = check_testset_enabled()
   argparser = setup( testset=testset, return_parser=True )
   
   # get RPC server options
   subparsers = argparser.add_subparsers(
      dest='action', help='the action to be taken')
   
   parser = subparsers.add_parser(
      'start',
      help='start the blockstored server')
   parser.add_argument(
      '--foreground', action='store_true',
      help='start the blockstored server in foreground')
   parser.add_argument(
      '--testset', action='store_true',
      help='run with the set of name operations used for testing, instead of the main set')
   
   parser = subparsers.add_parser(
      'stop',
      help='stop the blockstored server')
   parser.add_argument(
      '--testset', action='store_true',
      help='required if the daemon is using the testing set of name operations')
   
   parser = subparsers.add_parser(
      'reconfigure',
      help='reconfigure the blockstored server')
   parser.add_argument(
      '--testset', action='store_true',
      help='required if the daemon is using the testing set of name operations')
   
   parser = subparsers.add_parser(
      'clean',
      help='remove all blockstore database information')
   parser.add_argument(
      '--force', action='store_true',
      help='Do not confirm the request to delete.')
   parser.add_argument(
      '--testset', action='store_true',
      help='required if the daemon is using the testing set of name operations')
   
   parser = subparsers.add_parser(
      'indexer',
      help='run blockstore indexer worker')
   parser.add_argument(
      '--testset', action='store_true',
      help='required if the daemon is using the testing set of name operations')
   
   parser = subparsers.add_parser(
      'verify',
      help='verify an untrusted database against a known-good consensus hash')
   parser.add_argument(
      'consensus_hash',
      help='the known-good consensus hash')
   parser.add_argument(
      'db_path',
      help='the path to the database file')

   parser = subparsers.add_parser(
      'version',
      help='Print version and exit')
   
   args, _ = argparser.parse_known_args()
   
   log.debug( "bitcoin options: %s" % bitcoin_opts )
   
   if args.action == 'version':
      print "Blockstore version: %s" % VERSION
      print "Namespace ruleset version: %s" % BLOCKSTORE_VERSION
      print "Testset: %s" % testset
      sys.exit(0)
       
   if args.action == 'start':
      
      if os.path.exists( get_pidfile_path() ):
          log.error("Blockstored appears to be running already.  If not, please run '%s stop'" % (sys.argv[0]))
          sys.exit(1)
          
      if args.foreground:
         
         log.info('Initializing blockstored server in foreground (testset = %s)...' % testset)
         exit_status = run_server( foreground=True, testset=testset )
         log.info("Service endpoint exited with status code %s" % exit_status )
         
      else:
         
         log.info('Starting blockstored server (testset = %s) ...' % testset)
         run_server( testset=testset )
         
   elif args.action == 'stop':
      stop_server()

   elif args.action == 'reconfigure':
      reconfigure( testset=testset )
   
   elif args.action == 'clean':
      clean( confirm=(not args.force), testset=args.testset )
      
   elif args.action == 'indexer':
      run_indexer( testset=args.testset )

   elif args.action == 'verify':
      verify_database( args.consensus_hash, args.db_path )

if __name__ == '__main__':
    
   run_blockstored()
