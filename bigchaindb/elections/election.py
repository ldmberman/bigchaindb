# Copyright BigchainDB GmbH and BigchainDB contributors
# SPDX-License-Identifier: (Apache-2.0 AND CC-BY-4.0)
# Code is Apache-2.0 and docs are CC-BY-4.0
from collections import OrderedDict

import base58
from uuid import uuid4

from bigchaindb import backend
from bigchaindb.elections.vote import Vote
from bigchaindb.common.exceptions import (InvalidSignature,
                                          MultipleInputsError,
                                          InvalidProposer,
                                          UnequalValidatorSet,
                                          DuplicateTransaction)
from bigchaindb.tendermint_utils import key_from_base64, public_key_to_base64
from bigchaindb.common.crypto import (public_key_from_ed25519_key)
from bigchaindb.common.transaction import Transaction
from bigchaindb.common.schema import (_validate_schema,
                                      TX_SCHEMA_COMMON,
                                      TX_SCHEMA_CREATE)


class Election(Transaction):
    """Represents election transactions.

       To implement a custom election, create a class deriving from this one
       with OPERATION set to the election operation, ALLOWED_OPERATIONS
       set to (OPERATION,), CREATE set to OPERATION.
    """

    OPERATION = None
    # Custom validation schema
    TX_SCHEMA_CUSTOM = None
    # Election Statuses:
    ONGOING = 'ongoing'
    CONCLUDED = 'concluded'
    INCONCLUSIVE = 'inconclusive'
    # Vote ratio to approve an election
    ELECTION_THRESHOLD = 2 / 3

    @classmethod
    def get_validator_change(cls, bigchain):
        """Return the validator set from the most recent approved block

        :return: {
            'height': <block_height>,
            'validators': <validator_set>
        }
        """
        latest_block = bigchain.get_latest_block()
        if latest_block is None:
            return None
        return bigchain.get_validator_change(latest_block['height'])

    @classmethod
    def get_validators(cls, bigchain, height=None):
        """Return a dictionary of validators with key as `public_key` and
           value as the `voting_power`
        """
        validators = {}
        for validator in bigchain.get_validators(height):
            # NOTE: we assume that Tendermint encodes public key in base64
            public_key = public_key_from_ed25519_key(key_from_base64(validator['public_key']['value']))
            validators[public_key] = validator['voting_power']

        return validators

    @classmethod
    def recipients(cls, bigchain):
        """Convert validator dictionary to a recipient list for `Transaction`"""

        recipients = []
        for public_key, voting_power in cls.get_validators(bigchain).items():
            recipients.append(([public_key], voting_power))

        return recipients

    @classmethod
    def is_same_topology(cls, current_topology, election_topology):
        voters = {}
        for voter in election_topology:
            if len(voter.public_keys) > 1:
                return False

            [public_key] = voter.public_keys
            voting_power = voter.amount
            voters[public_key] = voting_power

        # Check whether the voters and their votes is same to that of the
        # validators and their voting power in the network
        return current_topology == voters

    def validate(self, bigchain, current_transactions=[]):
        """Validate election transaction

        NOTE:
        * A valid election is initiated by an existing validator.

        * A valid election is one where voters are validators and votes are
          allocated according to the voting power of each validator node.

        Args:
            :param bigchain: (BigchainDB) an instantiated bigchaindb.lib.BigchainDB object.
            :param current_transactions: (list) A list of transactions to be validated along with the election

        Returns:
            Election: a Election object or an object of the derived Election subclass.

        Raises:
            ValidationError: If the election is invalid
        """
        input_conditions = []

        duplicates = any(txn for txn in current_transactions if txn.id == self.id)
        if bigchain.is_committed(self.id) or duplicates:
            raise DuplicateTransaction('transaction `{}` already exists'
                                       .format(self.id))

        if not self.inputs_valid(input_conditions):
            raise InvalidSignature('Transaction signature is invalid.')

        current_validators = self.get_validators(bigchain)

        # NOTE: Proposer should be a single node
        if len(self.inputs) != 1 or len(self.inputs[0].owners_before) != 1:
            raise MultipleInputsError('`tx_signers` must be a list instance of length one')

        # NOTE: Check if the proposer is a validator.
        [election_initiator_node_pub_key] = self.inputs[0].owners_before
        if election_initiator_node_pub_key not in current_validators.keys():
            raise InvalidProposer('Public key is not a part of the validator set')

        # NOTE: Check if all validators have been assigned votes equal to their voting power
        if not self.is_same_topology(current_validators, self.outputs):
            raise UnequalValidatorSet('Validator set much be exactly same to the outputs of election')

        return self

    @classmethod
    def generate(cls, initiator, voters, election_data, metadata=None):
        # Break symmetry in case we need to call an election with the same properties twice
        uuid = uuid4()
        election_data['seed'] = str(uuid)

        (inputs, outputs) = cls.validate_create(initiator, voters, election_data, metadata)
        election = cls(cls.OPERATION, {'data': election_data}, inputs, outputs, metadata)
        cls.validate_schema(election.to_dict())
        return election

    @classmethod
    def validate_schema(cls, tx):
        """Validate the election transaction. Since `ELECTION` extends `CREATE` transaction, all the validations for
        `CREATE` transaction should be inherited
        """
        _validate_schema(TX_SCHEMA_COMMON, tx)
        _validate_schema(TX_SCHEMA_CREATE, tx)
        if cls.TX_SCHEMA_CUSTOM:
            _validate_schema(cls.TX_SCHEMA_CUSTOM, tx)

    @classmethod
    def create(cls, tx_signers, recipients, metadata=None, asset=None):
        raise NotImplementedError

    @classmethod
    def transfer(cls, tx_signers, recipients, metadata=None, asset=None):
        raise NotImplementedError

    @classmethod
    def to_public_key(cls, election_id):
        return base58.b58encode(bytes.fromhex(election_id)).decode()

    @classmethod
    def count_votes(cls, election_pk, transactions, getter=getattr):
        votes = 0
        for txn in transactions:
            if getter(txn, 'operation') == Vote.OPERATION:
                for output in getter(txn, 'outputs'):
                    # NOTE: We enforce that a valid vote to election id will have only
                    # election_pk in the output public keys, including any other public key
                    # along with election_pk will lead to vote being not considered valid.
                    if len(getter(output, 'public_keys')) == 1 and [election_pk] == getter(output, 'public_keys'):
                        votes = votes + int(getter(output, 'amount'))
        return votes

    def get_commited_votes(self, bigchain, election_pk=None):
        if election_pk is None:
            election_pk = self.to_public_key(self.id)
        txns = list(backend.query.get_asset_tokens_for_public_key(bigchain.connection,
                                                                  self.id,
                                                                  election_pk))
        return self.count_votes(election_pk, txns, dict.get)

    def has_concluded(self, bigchain, current_votes=[]):
        """Check if the election can be concluded or not.

        * Elections can only be concluded if the validator set has not changed
          since the election was initiated.
        * Elections can be concluded only if the current votes form a supermajority.

        Custom elections may override this function and introduce additional checks.
        """

        election_pk = self.to_public_key(self.id)
        votes_committed = self.get_commited_votes(bigchain, election_pk)
        votes_current = self.count_votes(election_pk, current_votes)

        if self.has_validator_set_changed(bigchain):
            return False

        current_validators = self.get_validators(bigchain)
        total_votes = sum(current_validators.values())
        if (votes_committed < (2/3) * total_votes) and \
                (votes_committed + votes_current >= (2/3)*total_votes):
            return True

        return False

    def get_status(self, bigchain):
        concluded = self.get_election(self.id, bigchain)
        if concluded:
            return self.CONCLUDED

        return self.INCONCLUSIVE if self.has_validator_set_changed(bigchain) else self.ONGOING

    def has_validator_set_changed(self, bigchain):
        latest_change = self.get_validator_change(bigchain)
        if latest_change is None:
            return False

        latest_change_height = latest_change['height']

        blocks = bigchain.get_block_containing_tx(self.id)
        if not blocks:
            return False
        election_height = blocks[0]

        return latest_change_height > election_height

    def get_election(self, election_id, bigchain):
        result = bigchain.get_election(election_id)
        return result

    @classmethod
    def store_election_results(cls, bigchain, election, height):
        bigchain.store_election_results(height, election)

    def show_election(self, bigchain):
        data = self.asset['data']
        if 'public_key' in data.keys():
            data['public_key'] = public_key_to_base64(data['public_key']['value'])
        response = ''
        for k, v in data.items():
            if k != 'seed':
                response += f'{k}={v}\n'
        response += f'status={self.get_status(bigchain)}'

        return response

    @classmethod
    def approved_elections(cls, bigchain, new_height, txns):
        elections = OrderedDict()
        for tx in txns:
            if not isinstance(tx, Vote):
                continue
            election_id = tx.asset['id']
            if election_id not in elections:
                elections[election_id] = []
            elections[election_id].append(tx)

        validator_update = None
        for election_id, votes in elections.items():
            election = bigchain.get_transaction(election_id)
            if election is None:
                continue

            if not election.has_concluded(bigchain, votes):
                continue

            validator_update = election.on_approval(bigchain, new_height)
            election.store_election_results(bigchain, election, new_height)

        return [validator_update] if validator_update else []

    def on_approval(self, bigchain, new_height):
        raise NotImplementedError
