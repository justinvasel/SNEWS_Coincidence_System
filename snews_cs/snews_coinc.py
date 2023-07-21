from . import cs_utils
from .snews_db import Storage
import os, click
from datetime import datetime
from .alert_pub import AlertPublisher
import numpy as np
import pandas as pd
from hop import Stream
from . import snews_bot
from .cs_alert_schema import CoincidenceTierAlert
from .cs_remote_commands import CommandHandler
from .core.logging import getLogger
from .cs_email import send_email
from .snews_hb import HeartBeat
from .cs_stats import cache_false_alarm_rate
import sys
import random
import time
import adc.errors

log = getLogger(__name__)


# TODO: duplicate for a test-cache. Do not drop actual cache each time there are tests
class CoincidenceDataHandler:
    """
    This class handles all the incoming data to the SNEWS CS Cache,
    adding messages, organizing sub-groups, retractions and updating cache entries
    """

    def __init__(self):
        # define the col names of the cache df
        self.cache = pd.DataFrame(columns=[
            "_id", "detector_name", "received_time", "machine_time", "neutrino_time",
            'neutrino_time_as_datetime',
            "p_val", "meta", "sub_group", "neutrino_time_delta"])
        # keep track of updated sub groups
        self.updated = []
        self.msg_state = None
        # this dict is used to store the current state of each sub group in the cache, UPDATE, COINCIDENT, None, RETRACTION.
        self.sub_group_state = {}

    def add_to_cache(self, message):
        """
        Takes in SNEWS message and checks if it is a retraction, update or new addition to cache.
        This is the 'core' function of CoincidenceDataHandler
        Parameters
        ----------
        message : dict
            SNEWS Message, must be PT valid

        """

        # retraction
        if 'retract_latest' in message.keys():
            print('RETRACTING MESSAGE FROM')
            self.cache_retraction(retraction_message=message)
            return None  # break if message is meant for retraction
        message['neutrino_time_as_datetime'] = datetime.fromisoformat(message['neutrino_time'])
        # update
        if message['detector_name'] in self.cache['detector_name'].to_list():
            self._update_message(message)
        # regular add
        else:
            self._manage_cache(message)
            self.cache = self.cache.sort_values(by=['sub_group', 'neutrino_time_delta'], ignore_index=True)
            self.cache = self.cache.reset_index(drop=True)

    def _manage_cache(self, message):
        """
        This method will add a new message to cache, checks if:

            A)It is an initial message (to the entire cache) or if it:
            B)Forms a new sub-group (sends message to _check_coinc_in_subgroups)
            C)Is confident to a sub-group (sends message to _check_coinc_in_subgroups)

        Parameters
        ----------
        message

        """

        # if the cache is empty add the message to cache, declare state of sub group 0 as INITIAL
        if len(self.cache) == 0:
            print('Initial Message!!')
            message['neutrino_time_delta'] = 0
            message['sub_group'] = 0
            temp = pd.DataFrame([message])
            self.sub_group_state[0] = 'INITIAL'
            self.cache = pd.concat([self.cache, temp], ignore_index=True)
        # if the cache is not empty, check if the message is coincident with other sub groups
        else:
            self._check_coinc_in_subgroups(message)

    def _check_coinc_in_subgroups(self, message):
        """ This method either:

            A)Adds Message to an existing sub-group, if coincident with the initial signal


            B) If NOT coincident with any sub groups it creates two new sub groups,
            setting the message as their initial time.
            The new groups consist of coincident signals with earlier arrival time and
            later arrival times, respectively.
            Once created the new groups are checked to see if they are redundant,
            and if so then they are not added to the main cache.

        Parameters
        ----------
        message : dict
            SNEWS message

        """
        # grab the current sub group tags
        sub_group_tags = self.cache['sub_group'].unique()
        #  this boolean declares whether if the message is not coincident
        is_coinc = False
        for tag in sub_group_tags:
            # query the cache, select the current sub group
            sub_cache = self.cache.query('sub_group==@tag')
            #  reset the index, for the sake of keeping things organized
            sub_cache = sub_cache.reset_index(drop=True)
            # select the initial nu time of the sub group
            sub_ini_t = sub_cache['neutrino_time_as_datetime'][0]
            #  make the nu time delta series
            delta = (message['neutrino_time_as_datetime'] - sub_ini_t).total_seconds()
            #  if the message's nu time is within the coincidence window
            if 0 < delta <= 10.0:
                # to the message add the corresponding sub group and nu time delta
                message['sub_group'] = tag
                message['neutrino_time_delta'] = delta
                # turn the message into a pd df, this is for concating it to the cache
                temp = pd.DataFrame([message])
                # concat the message df to the cahce
                self.cache = pd.concat([self.cache, temp], ignore_index=True)
                #  set the message as coinc
                is_coinc = True
                #  declare the state the sub group to COINC_MSG
                self.sub_group_state[tag] = 'COINC_MSG'

        # if the message is not coincident with any of the sub groups create a new sub group
        if not is_coinc:
            # set the message's nu time, as the initial nu time
            new_ini_t = message['neutrino_time_as_datetime']
            # create the sub group tag
            new_sub_tag = len(sub_group_tags)
            #  turn the message into a df
            message_as_cache = pd.DataFrame([message])
            #  create a temp cache concat the message
            temp_cache = pd.concat([self.cache, message_as_cache], ignore_index=True)
            #  drop dublicates of detector name and nu time
            temp_cache = temp_cache.drop_duplicates(subset=['detector_name', 'neutrino_time'])
            # create  a new time delta
            temp_cache['neutrino_time_delta'] = (
                    pd.to_datetime(temp_cache['neutrino_time_as_datetime']) - new_ini_t).dt.total_seconds()
            # Make two subgroup one for early signal and post
            new_sub_group_early = temp_cache.query('-10 <= neutrino_time_delta <= 0')
            new_sub_group_post = temp_cache.query('0 <= neutrino_time_delta <= 10.0')
            # drop old sub-group col or pandas will scream at you
            new_sub_group_post = new_sub_group_post.drop(columns='sub_group', axis=0)
            new_sub_group_early = new_sub_group_early.drop(columns='sub_group', axis=0)
            # make new sub-group tag
            new_sub_group_early['sub_group'] = new_sub_tag
            new_sub_group_post['sub_group'] = new_sub_tag + 1
            # sort sub-group by nu time
            new_sub_group_early = new_sub_group_early.sort_values(by='neutrino_time_as_datetime')
            new_sub_group_post = new_sub_group_post.sort_values(by='neutrino_time_as_datetime')
            #  organize the cache
            self._organize_cache(sub_cache=new_sub_group_post)
            self._organize_cache(sub_cache=new_sub_group_early)

    def _check_for_redundancies(self, sub_cache):
        """Checks if sub cache is redundant
        Parameters
        ----------
        sub_cache : dataframe
            New sub group

        Returns
        -------
        bool
            True if sub group is redundant
            False if sub cache is unique

        """
        # create a series of the ids in the sub group
        ids = sub_cache['_id']

        # if this sub group only contains a single message return True
        if len(sub_cache) == 1:
            return True
        #  loop through the other sub group tags
        for sub_tag in self.cache['sub_group'].unique():
            # save the other sub groups as a df
            other_sub = self.cache.query('sub_group == @sub_tag')
            # check if the current sub group's ids are in the other sub group
            check_ids = ids.isin(other_sub['_id'])
            # if the ids are in the other sub group, return True
            if check_ids.eq(True).all():
                return True
        return False

    def _organize_cache(self, sub_cache):
        """
        This method makes sure that the nu_delta_times are not negative,
        recalculates new deltas using the proper initial time

        Parameters
        ----------
        sub_cache : dataframe
            Sub group

        """
        #  if the sub is redundant then return out of the
        if self._check_for_redundancies(sub_cache):
            return
        # for the sake of keeping things organized reset the index of the sub group
        sub_cache = sub_cache.reset_index(drop=True)
        # if the initial nu time is negative then fix it by passing the sub group to fix_deltas
        if sub_cache['neutrino_time_delta'][0] < 0:
            sub_cache = self._fix_deltas(sub_df=sub_cache)

        # concat to the cache
        self.cache = pd.concat([self.cache, sub_cache], ignore_index=True)
        #  sort the values of the cache by their sub group and nu time ( ascending order)
        self.cache = self.cache.sort_values(by=['sub_group', 'neutrino_time_as_datetime']).reset_index(drop=True)

        #  this might be useless .. comment
        # if len(sub_cache) > 1:
        #     self.sub_group_state[sub_cache['sub_group'][0]] = 'COINC_MSG'
        # else:
        #     self.sub_group_state[sub_cache['sub_group'][0]] = 'INITIAL'

    def _fix_deltas(self, sub_df):
        """
        This methods fixes the deltas of the sub group by reseting the initial nu time
        Parameters
        ----------
        sub_df : Dataframe
            Sub cache

        Returns
        -------
        sub_df : Dataframe
            Sub cache with fixed nu time deltas

        """
        #  find the new initial nu time
        initial_time = sub_df['neutrino_time_as_datetime'].min()
        #  drop the old delta col
        sub_df = sub_df.drop(columns='neutrino_time_delta', axis=0)
        #  make the new delta col
        sub_df['neutrino_time_delta'] = (
                pd.to_datetime(sub_df['neutrino_time_as_datetime']) - initial_time).dt.total_seconds()
        #  sort the nu times by ascending order
        sub_df = sub_df.sort_values(by=['neutrino_time_as_datetime'])
        return sub_df

    def _update_message(self, message):
        """ If tirggered thhis method updates the p_val and neutrino time of a detector in cache.

        Parameters
        ----------
        message : dict
            SNEWS message

        Returns
        -------

        """

        # declare the name of the detector that will be updated
        update_detector = message["detector_name"]
        # announce that an update is happening
        update_message = f'\t> UPDATING MESSAGE FROM: {update_detector}'
        log.info(update_message)
        # get indices of where the detector name is present
        detector_ind = self.cache.query(f'detector_name==@update_detector').index.to_list()
        #  loop through the indices
        for ind in detector_ind:
            # get the sub tag
            sub_tag = self.cache['sub_group'][ind]
            #  declare the state of the sub group as UPDATE
            self.sub_group_state[sub_tag] = 'UPDATE'
            #  get the initial nu time of the sub group
            initial_time = self.cache.query('sub_group==@sub_tag')['neutrino_time_as_datetime'].min()
            # ignore update if the updated message is outside the coincident window
            if abs((message['neutrino_time_as_datetime'] - initial_time).total_seconds()) > 10.0:
                continue
            # update the message if it is coincident with the current sub group
            else:
                #  find the ind to be updated and replace its contents with
                for key in message.keys():
                    self.cache.at[ind, key] = message[key]
                self.cache.at[ind, 'neutrino_time_delta'] = (
                        message['neutrino_time_as_datetime'] - initial_time).total_seconds()
                # append the updated list
                self.updated.append(self.cache['sub_group'][ind])

        # if there are any updated sub groups reorganize them
        if len(self.updated) != 0:
            # loop through updated sub group list
            for sub_tag in self.updated:
                #  make a sub group df
                sub_df = self.cache.query('sub_group == @sub_tag')
                # dump the unorganized subgroup
                self.cache = self.cache.query('sub_group != @sub_tag')
                # fix deltas of updated sub group
                sub_df = self._fix_deltas(sub_df)
                # concat the organized sub group with the rest of the cache
                self.cache = pd.concat([self.cache, sub_df], ignore_index=True)
                #  sort the values of the cache by sub group nu time
                self.cache = self.cache.sort_values(
                    by=['sub_group', 'neutrino_time_as_datetime']).reset_index(drop=True)

    def cache_retraction(self, retraction_message):
        """
        This method handdles message retraction by parsing the cache and dumping any instance of the target detector


        Parameters
        ----------
        retraction_message : dict
            SNEWS retraction message

        """

        retracted_name = retraction_message['detector_name']
        self.cache = self.cache.query('detector_name!=@retracted_name')
        logstr = retracted_name
        # in case retracted message was an initial
        if len(self.cache) == 0:
            return 0
        for sub_tag in self.cache['sub_group'].unique():
            self.sub_group_state[sub_tag] = 'RETRACTION'
            other_sub = self.cache.query('sub_group == @sub_tag')
            if other_sub['neutrino_time_delta'].min() != 0.0:
                if len(other_sub) == 1:
                    other_sub = other_sub.drop(columns=['neutrino_time_delta'])
                    other_sub['neutrino_time_delta'] = [0]

                else:
                    # set new initial nu time
                    new_initial_time = pd.to_datetime(other_sub['neutrino_time_as_datetime'].min())
                    # drop the old delta
                    other_sub = other_sub.drop(columns=['neutrino_time_delta'])
                    #  make new delta
                    other_sub['neutrino_time_delta'] = (pd.to_datetime(
                        other_sub['neutrino_time_as_datetime']) - new_initial_time).dt.total_seconds()
                # concat retracted sub group to the cache
                self.cache = self.cache.query('sub_group!=@sub_tag')
                self.cache = pd.concat([self.cache, other_sub], ignore_index=True)
                self.cache = self.cache.sort_values(by='neutrino_time').reset_index()
            # log retraction to log file
            log.info(f"\t> Retracted {logstr} from sub-group {sub_tag}")


class CoincidenceDistributor:


    def __init__(self, env_path=None, use_local_db=True, drop_db=False, firedrill_mode=True, hb_path=None,
                 server_tag=None, send_email=False, send_slack=True, show_table = False):
        """This class is in charge of sending alerts to SNEWS when CS is triggered

        Parameters
        ----------
        env_path : `str`
            path to env file, defaults to '/auxiliary/test-config.env'
        use_local_db: `bool`
            tells CoincDecider to use local MongoClient, defaults to True
        send_slack: `bool`
            Whether to send alerts on slack

        """
        log.debug("Initializing CoincDecider\n")
        cs_utils.set_env(env_path)
        self.show_table = show_table
        self.send_email = send_email
        self.send_slack = send_slack
        self.hb_path = hb_path
        # name of your sever, used for alerts
        self.server_tag = server_tag
        # initialize local MongoDB
        self.storage = Storage(drop_db=drop_db, use_local_db=use_local_db)
        # declare topic type, used for alerts
        self.topic_type = "CoincidenceTier"
        #  from the env var get the coinc thresh, 10sec
        self.coinc_threshold = float(os.getenv('COINCIDENCE_THRESHOLD'))
        # lifetime of case (sec) = 24hr
        self.cache_expiration = 86400
        # Some Kafka errors are retryable.
        self.retriable_error_count = 0
        self.max_retriable_errors = 20
        self.exit_on_error = False  # True
        self.initial_set = False
        self.alert = AlertPublisher(env_path=env_path, use_local=use_local_db, firedrill_mode=firedrill_mode)
        if firedrill_mode:
            self.observation_topic = os.getenv("FIREDRILL_OBSERVATION_TOPIC")
        else:
            self.observation_topic = os.getenv("OBSERVATION_TOPIC")
        self.alert_schema = CoincidenceTierAlert(env_path)
        # handle heartbeat
        self.store_heartbeat = bool(os.getenv("STORE_HEARTBEAT", "True"))
        self.heartbeat = HeartBeat(env_path=env_path, firedrill_mode=firedrill_mode)

        self.stash_time = 86400
        self.coinc_data = CoincidenceDataHandler()

    def clear_cache(self):
        """ When a reset cache is passed, recreate the
            CoincidenceDataHandler instance

        """
        log.info("\t > [RESET] Resetting the cache.")
        del self.coinc_data
        self.coinc_data = CoincidenceDataHandler()

    # ----------------------------------------------------------------------------------------------------------------
    def display_table(self):
        """
        Display each sub list individually using a markdown table.

        """
        click.secho(
            f'Here is the current coincident table\n',
            fg='magenta', bold=True, )
        for sub_list in self.coinc_data.cache['sub_group'].unique():
            sub_df = self.coinc_data.cache.query(f'sub_group=={sub_list}')
            sub_df = sub_df.drop(columns=['meta', 'machine_time', 'schema_version', 'neutrino_time_as_datetime'])
            sub_df = sub_df.sort_values(by=['neutrino_time'])
            # snews_bot.send_table(sub_df) # no need to print the table on the server. Logs have the full content
            print(sub_df.to_markdown())
            print('=' * 168)

    def send_alert(self, sub_group_tag, alert_type):
        sub_df = self.coinc_data.cache.query('sub_group==@sub_group_tag')
        p_vals = sub_df['p_val'].to_list()
        p_vals_avg = np.round(sub_df['p_val'].mean(), decimals=5)
        nu_times = sub_df['neutrino_time'].to_list()
        detector_names = sub_df['detector_name'].to_list()
        false_alarm_prob = cache_false_alarm_rate(cache_sub_list=sub_df, hb_cache=self.heartbeat.cache_df)

        alert_data = dict(p_vals=p_vals,
                          p_val_avg=p_vals_avg,
                          sub_list_num=sub_group_tag,
                          neutrino_times=nu_times,
                          detector_names=detector_names,
                          false_alarm_prob=false_alarm_prob,
                          server_tag=self.server_tag,
                          alert_type=alert_type)

        with self.alert as pub:
            alert = self.alert_schema.get_cs_alert_schema(data=alert_data)
            pub.send(alert)
            if self.send_email:
                send_email(alert)
            if self.send_slack:
                snews_bot.send_table(alert_data,
                                     alert,
                                     is_test=True,
                                     topic=self.observation_topic)

    # ------------------------------------------------------------------------------------------------------------------
    def alert_decider(self):
        """
        This method will publish an alert every time a new detector
        submits an observation message

        """
        # mkae a pretty terminal output
        click.secho(f'{"=" * 100}', fg='bright_red')
        # loop through the sub group tag and state
        for sub_group_tag, state in self.coinc_data.sub_group_state.items():
            # if state is none skip the sub group
            if state is None:
                continue
            # publish a retraction alert for the sub group is its state is RETRACTION
            elif state == 'RETRACTION':
                #  yet another pretty terminal output
                click.secho(f'SUB GROUP {sub_group_tag}:{"RETRACTION HAS BEEN MADE".upper():^100}', bg='bright_green',
                            fg='red')
                click.secho(f'{"Publishing an updated  alert..".upper():^100}', bg='bright_green', fg='red')
                click.secho(f'{"=" * 100}', fg='bright_red')
                # publish retraction alert
                self.send_alert(sub_group_tag=sub_group_tag, alert_type=state)
                continue
            #Don't publish alert for the sub group is its state is INITIAL
            elif state == 'INITIAL':
                #  yet another pretty terminal output
                log.debug(f'\t> Initial message in sub group:{sub_group_tag}')
                click.secho(f'SUB GROUP {sub_group_tag}:{"Initial message recieved".upper():^100}', bg='bright_green',
                            fg='red')
                click.secho(f'{"=" * 100}', fg='bright_red')
                continue
            elif state == 'UPDATE':
                #  yet another pretty terminal output
                click.secho(f'SUB GROUP {sub_group_tag}:{"A MESSGAE HAS BEEN UPDATED".upper():^100}', bg='bright_green',
                            fg='red')
                click.secho(f'{"Publishing an updated  Alert!!!".upper():^100}', bg='bright_green', fg='red')
                click.secho(f'{"=" * 100}', fg='bright_red')
                log.debug('\t> An UPDATE message is received')
                # publish update alert
                self.send_alert(sub_group_tag=sub_group_tag, alert_type=state)
                log.debug('\t> An alert is updated!')
                continue
            elif state == 'COINC_MSG':
                #  yet another pretty terminal output
                click.secho(f'SUB GROUP {sub_group_tag}:{"NEW COINCIDENT DETECTOR.. ".upper():^100}', bg='bright_green', fg='red')
                click.secho(f'{"Published an Alert!!!".upper():^100}', bg='bright_green', fg='red')
                click.secho(f'{"=" * 100}', fg='bright_red')
                # publish coincidence alert
                log.info(f"\t> An alert was published: {state} !")
                self.send_alert(sub_group_tag=sub_group_tag, alert_type=state)
                continue

    # ------------------------------------------------------------------------------------------------------------------
    def run_coincidence(self):
        """
        As the name states this method runs the coincidence system.
        Starts by subscribing to the hop observation_topic.

        * If a CoincidenceTier message is received then it is passed to _check_coincidence.
        * other commands include "test-connection", "test-scenarios",
                "hard-reset", "Retraction",

        ****
        Reconnect logic and retryable errors thanks to Spencer Nelson (https://github.com/spenczar)
        https://github.com/scimma/hop-client/issues/140

        """
        fatal_error = True

        while True:
            try:
                stream = Stream(until_eos=False)
                with stream.open(self.observation_topic, "r") as s:
                    click.secho(f'{datetime.utcnow().isoformat()} (re)Initializing Coincidence System for '
                                f'{self.observation_topic}\n')
                    for snews_message in s:
                        # check for the hop version
                        try:
                            snews_message = snews_message.content
                        except Exception as e:
                            log.error(f"A message with older hop version is found. {e}\n{snews_message}")
                            snews_message = snews_message
                        # handle the input message
                        handler = CommandHandler(snews_message)
                        # if a coincidence tier message (or retraction) run through the logic
                        if handler.handle(self):
                            snews_message['received_time'] = datetime.utcnow().isoformat()
                            click.secho(f'{"-" * 57}', fg='bright_blue')
                            self.coinc_data.add_to_cache(message=snews_message)
                            if self.show_table:
                                self.display_table() ## don't display on the server
                            self.alert_decider()
                            self.storage.insert_mgs(snews_message)
                            sys.stdout.flush()
                            # reset state of each sub group
                            for key in self.coinc_data.sub_group_state.keys():
                                self.coinc_data.sub_group_state[key] = None
                            self.coinc_data.updated = []

                        # for each read message reduce the retriable err count
                        if self.retriable_error_count > 1:
                            self.retriable_error_count -= 1

            # if there is a KafkaException, check if retriable
            except adc.errors.KafkaException as e:
                if e.retriable:
                    self.retriable_error_count += 1
                    if self.retriable_error_count >= self.max_retriable_errors:
                        log.error(f"Max retryable errors exceeded. Here is the most recent exception:\n{e}\n")
                        fatal_error = True
                    else:
                        log.error(f"Retryable error! \n{e}\n")
                        # sleep with exponential backoff and a bit of jitter.
                        time.sleep((1.5 ** self.retriable_error_count) * (1 + random.random()) / 2)
                else:
                    log.error(
                        f"(1) Something crashed the server, not a retriable error, here is the Exception raised\n{e}\n")
                    fatal_error = True

            # any other exception is logged, but not fatal (?)
            except Exception as e:
                log.error(f"(2) Something crashed the server, here is the Exception raised\n{e}\n")
                fatal_error = False  # True # maybe not a fatal error?

            finally:
                # if we are breaking on errors and there is a fatal error, break
                if self.exit_on_error and fatal_error:
                    break
                # otherwise continue by re-initiating
                continue
