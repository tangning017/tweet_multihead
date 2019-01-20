from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import logging.config
import sys
import numpy as np
import reader
from utils import eval_res

info = "newsandprice"
logger = logging.getLogger('my_logger')
logger.setLevel(logging.DEBUG)
log_fp = '{0}.log'.format(f'{info}_model')
file_handler = logging.FileHandler(log_fp)
console_handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# root_path = "data/tweets/file/"
# EMBEDDING_DIM = 50
# HIDDEN_SIZE = 16
# NUM_LAYERS = 1
# NUM_CLASS = 1
# PREDICT_STEPS = 1
# NUM_EPOCH = 10
# LINEAR_DIM = 64
# DECAY_STEP = 10
# DECAY_RATE = 0.96
# STOCK_SIZE = 87
# FEATURE_NUM = 4

root_path = "data/news/file/"
EMBEDDING_DIM = 300
HIDDEN_SIZE = 16
NUM_LAYERS = 1
NUM_CLASS = 1
PREDICT_STEPS = 1
NUM_EPOCH = 15
LINEAR_DIM = 64
DECAY_STEP = 10
DECAY_RATE = 0.96
STOCK_SIZE = 200
FEATURE_NUM = 9


class StockMovementPrediction(object):
    def __init__(self, batch_size, num_steps, linear_dim, num_head, drop_out,
                 max_num_news, max_num_words, lr, vocab_size, att_lambda, param_lambda):
        self.linear_dim = linear_dim
        self.num_head = num_head
        self.batch_size = batch_size
        self.num_steps = num_steps
        self.drop_out = drop_out
        self.max_num_news = max_num_news
        self.max_num_words = max_num_words
        self.lr = lr
        self.hidden_size = HIDDEN_SIZE
        self.att_lambda = att_lambda
        self.param_lambda = param_lambda
        self.attention_reg = []
        self.final_state = None
        self.initial_state = None

        assert self.linear_dim % self.num_head == 0

        with tf.name_scope('input'):
            self.input_data = tf.placeholder(tf.float32, [self.batch_size, self.num_steps, FEATURE_NUM], name="price")
            self.targets = tf.placeholder(tf.float32, [self.batch_size, PREDICT_STEPS], name='rate')
            self.news_ph = tf.placeholder(tf.int64, [self.batch_size, self.num_steps, self.max_num_news,
                                                     self.max_num_words], name='news')
            self.word_table_init = tf.placeholder(tf.float32, [vocab_size, EMBEDDING_DIM], name='word_embedding')
            self.is_training = tf.placeholder(tf.bool, shape=(), name="train")
        with tf.name_scope('word_embeddings'):
            with tf.variable_scope('embeds'):
                word_table = tf.get_variable('word_table', initializer=self.word_table_init, trainable=False)
                self.news = tf.nn.embedding_lookup(word_table, self.news_ph, name='news_word_embeds')
                self.news = tf.layers.dense(self.news, self.hidden_size, use_bias=False) # reduce the dimension of words

        logger.info(
            f"embedding_size:{EMBEDDING_DIM}, max_num_news:{self.max_num_news}, max_num_words:{self.max_num_words},"
            f" lr:{self.lr}, batch_size:{self.batch_size}, num_head:{self.num_head}, drop_out:{self.drop_out},"
            f" num_step:{self.num_steps}, att_lambda: {self.att_lambda}, param_lambda: {self.param_lambda}")

        outputs = self.encode()
        logits = self.decode(outputs, self.final_state)
        with tf.name_scope("loss_function"):
            self.mse_loss = tf.losses.mean_squared_error(labels=self.targets, predictions=tf.squeeze(logits))
            self.rmse_loss = tf.sqrt(self.mse_loss)
            # self.cross_entropy = tf.losses.sparse_softmax_cross_entropy(labels=self.targets, logits=logits)
            trainable_vars = tf.trainable_variables()
            self.param_loss = self.param_lambda * tf.reduce_mean([tf.nn.l2_loss(v) for v in trainable_vars if 'bias' not in v.name])
            if len(self.attention_reg) == 0:
                self.attention_reg = [0.0]
            self.att_loss = self.att_lambda * tf.reduce_mean(self.attention_reg)
            self.loss = self.rmse_loss + self.att_loss + self.param_loss
            self.prediction = logits

        if self.is_training is None:
            return

        # Optimizer #
        global_step = tf.Variable(0, trainable=False)
        learning_rate = tf.train.exponential_decay(self.lr, global_step, DECAY_STEP, DECAY_RATE, staircase=True)
        tf.summary.scalar('learning_rate', learning_rate)
        self.train_op = tf.train.AdamOptimizer(learning_rate).minimize(self.loss, global_step=global_step)

    def encode(self):
        """
        :return:
        """
        encoder_cell = tf.contrib.rnn.LSTMCell(self.hidden_size)
        cell = tf.nn.rnn_cell.MultiRNNCell([encoder_cell] * NUM_LAYERS)
        self.initial_state = cell.zero_state(self.batch_size, tf.float32)

        outputs = []
        state = self.initial_state

        with tf.variable_scope("encoder"):
            for time_step in range(self.num_steps):
                daily_news = []
                with tf.variable_scope("word_level_bidirectional_lstm", reuse=tf.AUTO_REUSE):
                    for num_news in range(self.max_num_news):
                        cell_fw = tf.nn.rnn_cell.LSTMCell(self.hidden_size)
                        cell_bw = tf.nn.rnn_cell.LSTMCell(self.hidden_size)
                        bioutput, _ = tf.nn.bidirectional_dynamic_rnn(
                        cell_fw, cell_bw, self.news[:, time_step, num_news, :, :], dtype=tf.float32, time_major=False)
                        bilstm_output = (bioutput[0] + bioutput[1]) / 2
                        bilstm_output = tf.reduce_mean(bilstm_output, axis=-2)
                        daily_news.append(bilstm_output)
                    daily_news = tf.transpose(daily_news, [1, 0, 2])
                with tf.variable_scope('news_level_attention', reuse=tf.AUTO_REUSE):
                    att_daily_news = self._multi_head_transform(daily_news, self.num_head, self.linear_dim)
                with tf.variable_scope("aspect_level_attention", reuse=tf.AUTO_REUSE):
                    att_daily_aspect, alpha = self._single_attention(att_daily_news, state[0].h)
                price = self.input_data[:, time_step, :]
                cell_input = tf.concat([price, att_daily_aspect], axis=-1)
                # cell_input = att_daily_aspect
                cell_input = tf.contrib.layers.batch_norm(cell_input)
                if self.is_training is not None:
                    cell_input = tf.layers.dropout(cell_input, rate=self.drop_out)
                cell_output, state = cell(cell_input, state)
                outputs.append(cell_output)
        self.final_state = state
        return outputs

    def decode(self, encode, state):
        """
        multi step stock movement prediction
        encode： encode hidden states
        state: final hidden states of encoder
        return the predicted logits
        """
        with tf.variable_scope("decoder"):
            # [batch_size, max_time, num_units]
            attention_states = tf.transpose(encode, [1, 0, 2])
            attention_mechanism = tf.contrib.seq2seq.LuongAttention(self.hidden_size, attention_states)

            decoder_cell = tf.contrib.rnn.LSTMCell(self.hidden_size)
            decoder_cell = tf.contrib.seq2seq.AttentionWrapper(decoder_cell, attention_mechanism,
                                                               attention_layer_size=self.hidden_size)

            helper = tf.contrib.seq2seq.TrainingHelper(encode, [PREDICT_STEPS for _ in range(self.batch_size)],
                                                       time_major=True)
            decoder_initial_state = decoder_cell.zero_state(self.batch_size, tf.float32).clone(cell_state=state[0])
            decoder = tf.contrib.seq2seq.BasicDecoder(decoder_cell, helper, decoder_initial_state)
            outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoder)
            outputs = outputs.rnn_output
            outputs = tf.contrib.layers.batch_norm(outputs)
            if self.is_training is not None:
                outputs = tf.layers.dropout(outputs, rate=self.drop_out)
            logits = tf.layers.dense(outputs, NUM_CLASS)
        return logits

    @staticmethod
    def _multi_head_transform(v, num_head, dim):
        with tf.name_scope('multi_head_single_attention'):
            # linear projection
            with tf.variable_scope('linear_projection'):
                vp = tf.layers.dense(v, dim, use_bias=False)
            # split_heads
            with tf.variable_scope('split_head'):
                def split_last_dimension_then_transpose(tensor, num_head, dim):
                    t_shape = tensor.get_shape().as_list()
                    tensor = tf.reshape(tensor, [-1] + t_shape[1:-1] + [num_head, dim])
                    return tf.transpose(tensor, [0, 2, 1, 3])  # [batch_size, num_heads, seq_len, dim]

                vs_q = split_last_dimension_then_transpose(vp, num_head, dim // num_head)

            multi_heads_output = tf.reduce_max(vs_q, axis=-2)
        return multi_heads_output

    @staticmethod
    def _single_attention(v, k):
        seq_len = v.get_shape().as_list()[-2]
        hidden_dim = k.get_shape().as_list()[-1]
        v_pro = tf.layers.dense(v, hidden_dim, use_bias=False, activation=tf.nn.tanh)
        k_ext = tf.tile(k, [1, seq_len])  # [batch_size, seq_len * hidden_dim]
        k_ext = tf.reshape(k_ext, [-1, seq_len, hidden_dim])
        att = tf.reduce_sum(tf.multiply(v_pro, k_ext), axis=-1)

        att = tf.nn.softmax(att)    # [batch_size, seq_len]
        tf.summary.histogram('aspect_attention', att)
        attention_output = tf.reduce_sum(v * tf.expand_dims(att, -1), -2)  # [batch_size, dim]
        return attention_output, att

    @staticmethod
    def _pooling(v):
        return tf.reduce_max(v, axis=-3)


def run_epoch(session, merged, model, data, flag, output_log):
    total_costs = []
    att_costs = []
    param_costs = []
    state = session.run(model.initial_state)
    predictions = []
    ys = []
    iters = 0
    for x, y, _, news, _ in reader.news_iterator(data, model.batch_size, model.num_steps,
                                                 model.max_num_news, model.max_num_words, flag):
        fetch = [model.loss, model.att_loss, model.param_loss,
                 model.final_state, merged, model.prediction]
        feed_dict = {model.input_data: x, model.targets: y, model.news_ph: news, model.initial_state: state}
        if flag == 'train':
            feed_dict[model.is_training] = flag
            fetch.append(model.train_op)
        else:
            fetch.append(tf.no_op())

        cost, att_loss, param_loss, state, summary, prediction, _ = session.run(fetch, feed_dict)
        total_costs.append(cost)
        att_costs.append(att_loss)
        param_costs.append(param_loss)
        iters += model.num_steps
        if output_log and iters % 1000 == 0:
            logger.info(f"cost {cost}, att {att_loss}, param {param_loss}, {eval_res(y, prediction)}")
            sum_path = f'tensorboard/{flag}/%smodel_batch%d_h%d_d%.2f_step%d_news%d_words%d_lr%.5f'\
                       % (info, model.batch_size, model.num_head, model.drop_out, model.num_steps,
                          model.max_num_news, model.max_num_words, model.lr)
            writer = tf.summary.FileWriter(sum_path, tf.Session().graph)
            writer.add_summary(summary, iters)
        predictions.append(prediction)
        ys.append(y)
    return np.mean(total_costs), np.mean(att_costs), np.mean(param_costs), eval_res(predictions, ys)


def tuning_parameter():
    for num_step in [10]:
        for att_lambda in [0.001]:
            for param_lambda in [0.001]:
                for lr in [0.001]:
                    yield 10, 10, lr, 64, 4, 0.3, num_step, att_lambda, param_lambda


def main(_):
    train_data, valid_data, test_data = reader.news_raw_data()
    word_table_init, vocab_size = reader.init_word_table()
    parameter_gen = tuning_parameter()
    while True:
        try:
            max_num_news, max_num_words, lr, batch_size, num_head, drop_out, num_steps,\
                att_lambda, param_lambda = next(parameter_gen)
        except StopIteration:
            break
        initializer = tf.contrib.layers.xavier_initializer()
        tf.reset_default_graph()
       # if os.path.exists(os.path.join(root_path, f"{info}train_preprocess.pkl")):
       #     os.remove(os.path.join(root_path, f'{info}train_preprocess.pkl'))
       # if os.path.exists(os.path.join(root_path, f"{info}valid_preprocess.pkl")):
       #     os.remove(os.path.join(root_path, f'{info}valid_preprocess.pkl'))
       # if os.path.exists(os.path.join(root_path, f"{info}test_preprocess.pkl")):
       #     os.remove(os.path.join(root_path, f'{info}test_preprocess.pkl'))
        with tf.name_scope("Train"):
            with tf.variable_scope("StockMovementPrediction", reuse=None, initializer=initializer):
                model = StockMovementPrediction(batch_size, num_steps,
                                                LINEAR_DIM, num_head, drop_out, max_num_news,
                                                max_num_words, lr, vocab_size, att_lambda, param_lambda)
        saver = tf.train.Saver()
        merged = tf.summary.merge_all()
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        valid_mse = 1
        valid_acc = 0
        flag_cnt = 0
        with tf.Session(config=config) as session:
            init = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
            session.run(init, feed_dict={model.word_table_init: word_table_init})
            total_parameters = 0
            for variable in tf.trainable_variables():
                shape = variable.get_shape()
                variable_parameters = 1
                for dim in shape:
                    variable_parameters += dim.value
                total_parameters += variable_parameters
            logger.info("total parameters: %d", total_parameters)
            for i in range(NUM_EPOCH):
                train_cost, att_cost, param_cost, eval = \
                    run_epoch(session, merged, model, train_data, 'train', True)
                logger.info(logger.info(f"Epoch {i+1} train_cost {train_cost},"
                                        f" att {att_cost}, param {param_cost}, {eval}"))
                valid_cost, att_cost, param_cost, eval = \
                    run_epoch(session, merged, model, valid_data, 'valid', False)
                logger.info(
                    logger.info(f"Epoch {i + 1} valid_cost {valid_cost},"
                                f" att {att_cost}, param {param_cost}, {eval}"))
                if eval['mse'] > valid_mse or eval['acc'] > valid_acc:
                    saver.save(session, 'model_saver/%smodel_batch%d_h%d_d%.2f_step%d_news%d_words%d_lr%.5f.ckpt' %
                               (info, batch_size, num_head, drop_out, num_steps, max_num_news, max_num_words, lr))
                    valid_mse = eval['mse']
                    valid_acc = eval['acc']
                    flag_cnt = 0
                else:
                    if flag_cnt > 1:
                        break
                    flag_cnt += 1
        with tf.Session(config=config) as session:
            saver.restore(session, 'model_saver/%smodel_batch%d_h%d_d%.2f_step%d_news%d_words%d_lr%.5f.ckpt' %
                          (info, batch_size, num_head, drop_out, num_steps, max_num_news, max_num_words, lr))
            test_cost, att_cost, param_cost, acc = run_epoch(session, merged, model, test_data, 'test', False)
            logger.info(
                logger.info(f"test_cost {test_cost}, att {att_cost}, param {param_cost}, {eval}"))


if __name__ == "__main__":
    tf.app.run()
