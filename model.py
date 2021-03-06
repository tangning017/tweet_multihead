from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import reader
import os
import datetime
import logging
import logging.config
import sys
from sklearn.metrics import confusion_matrix
import math

info = "price"
logger = logging.getLogger('my_logger')
logger.setLevel(logging.DEBUG)
log_fp = '{0}.log'.format(f'{info}model')
file_handler = logging.FileHandler(log_fp)
console_handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

DATA_PATH = "file/"
EMBEDDING_DIM = 50
HIDDEN_SIZE = 64
NUM_LAYERS = 1
NUM_CLASS = 2
MAX_GRAD_NORM = 3
NUM_EPOCH = 15
LINEAR_DIM = 64
DECAY_STEP = 10
DECAY_RATE = 0.98
STOCK_SIZE = 87
LAMBDA = 0.05
FEATURE_NUM = 3
root_path = "data/tweet/"

class StockMovementPrediction(object):
    def __init__(self, is_training, batch_size, num_steps, linear_dim, num_head, drop_out, max_num_news, lr):
        self.linear_dim = linear_dim
        self.num_head = num_head
        self.batch_size = batch_size
        self.num_steps = num_steps
        self.is_training = is_training
        self.drop_out = drop_out
        self.max_num_news = max_num_news
        self.lr = lr
        self.att_loss = 0
        assert LINEAR_DIM == HIDDEN_SIZE

        with tf.name_scope('input'):
            self.input_data = tf.placeholder(tf.float32, [None, self.num_steps, FEATURE_NUM], name="price")
            self.targets = tf.placeholder(tf.int64, [None, self.num_steps], name='label')
            self.news = tf.placeholder(tf.float32, [None, self.num_steps, self.max_num_news, EMBEDDING_DIM], name='news')
            self.stock_id = tf.placeholder(tf.int64, [None], name='stocks')

        logger.info(f"embedding_size:{EMBEDDING_DIM}, max_num_news:{self.max_num_news},lr:{self.lr}, batch_size:{self.batch_size}, num_head:{self.num_head}, drop_out:{self.drop_out}, num_step:{self.num_steps}")
        
        with tf.variable_scope('embedding'):
            embedding = tf.get_variable("stock_embedding", [STOCK_SIZE, HIDDEN_SIZE])
            self.stock_embedding = tf.nn.embedding_lookup(embedding, self.stock_id)

#         lstm_cell = tf.nn.rnn_cell.LSTMCell(HIDDEN_SIZE)
        lstm_cell = tf.contrib.rnn.LayerNormBasicLSTMCell(HIDDEN_SIZE)
        cell = tf.nn.rnn_cell.MultiRNNCell([lstm_cell]*NUM_LAYERS)
        self.initial_state = cell.zero_state(self.batch_size, tf.float32)

        outputs = []

        state = self.initial_state
        with tf.variable_scope("RNN"):
            for time_step in range(self.num_steps):
                if time_step > 0:
                    tf.get_variable_scope().reuse_variables()
                # print(state)
                if info == "":
                    with tf.name_scope('multihead_attention'):
                        input1 = self._multi_head(self.news[:, time_step, :, :], state[0].h)
                        input1 = tf.concat([self.input_data[:, time_step, :], input1, self.stock_embedding], -1)
                    cell_output, state = cell(input1, state)
                else:
                    cell_output, state = cell(self.input_data[:, time_step, :], state)
#                 self.variable_summary(state, 'RNN/state')
                outputs.append(cell_output)
        with tf.variable_scope('MLP'):
            output = tf.reshape(outputs, [-1, HIDDEN_SIZE])
            weight = tf.get_variable('weight', [HIDDEN_SIZE, NUM_CLASS])
#             self.variable_summary(weight, 'MLP/weight')
            bias = tf.get_variable('bias', [NUM_CLASS])
#             self.variable_summary(bias, 'MLP/bias')
            logits = tf.matmul(output, weight) + bias
        with tf.name_scope('loss_function'):
            targets = tf.reshape(self.targets, [-1])
            loss = (1-LAMBDA)*tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=targets)+LAMBDA*self.att_loss
            self.acc = tf.reduce_mean(tf.cast(tf.equal(tf.argmax(logits, -1), targets), tf.float32))
            self.prediction = tf.argmax(logits, -1)
            self.cost = tf.reduce_sum(loss) / tf.cast(self.batch_size, tf.float32)
            tf.summary.scalar('accuarcy', self.acc)
            tf.summary.scalar('loss', self.cost)
        self.final_state = state

        if not self.is_training:
            return
        global_step = tf.Variable(0)
        learning_rate = tf.train.exponential_decay(self.lr, global_step, DECAY_STEP, DECAY_RATE, staircase=True)
        tf.summary.scalar('learning_rate', learning_rate)
        self.train_op = tf.train.AdamOptimizer(learning_rate).minimize(loss, global_step=global_step)
        
#         trainable_variables = tf.trainable_variables()
#         grads, _ = tf.clip_by_global_norm(tf.gradients(self.cost, trainable_variables), MAX_GRAD_NORM)

#         optimizer = tf.train.GradientDescentOptimizer(LEARNING_RATE)

#         self.train_op = optimizer.apply_gradients(zip(grads, trainable_variables))
    def variable_summary(self, var, name):
        with tf.name_scope('summaries'):
            tf.summary.histogram(name, var)
            mean = tf.reduce_mean(var)
            tf.summary.scalar('mean/' + name, mean)
            stddev = tf.sqrt(tf.reduce_sum(tf.square(var-mean)))
            tf.summary.scalar('stddev/' + name, stddev)

    def _multi_head(self, v, state):
        with tf.name_scope('transformer'):
            # linear projection
            with tf.variable_scope('linear_projection'):
                v = tf.tile(v, [1, 1, EMBEDDING_DIM*self.num_head])
                vp = tf.layers.dense(v, self.linear_dim*self.num_head, use_bias=False)

            # split_heads
            with tf.variable_scope('split_head'):
                def split_last_dimension_then_transpose(tensor, num_heads, dim):
                    t_shape = tensor.get_shape().as_list()
                    tensor = tf.reshape(tensor, [-1] + t_shape[1:-1] + [num_heads, dim])
                    return tf.transpose(tensor, [0, 2, 1, 3]) #[batch_size, num_heads, max_seq_len, dim]
                vs = split_last_dimension_then_transpose(vp, self.num_head, self.linear_dim)
            
            # scaled_dot_product
            with tf.variable_scope('scaled_dot_product'):
                st = tf.tile(state, [1, self.num_head*self.max_num_news])
                st = tf.reshape(st, [self.batch_size, self.num_head, self.max_num_news, HIDDEN_SIZE])
                ## [B, Head, max_sequence, dim] --> repeat last dims head* max_sequence times 
                o1 = tf.reduce_sum(tf.multiply(vs, st), axis=-1)
                multiatt = tf.nn.softmax(o1)
                ### pently
                a1 = tf.tile(multiatt, [1, 1, self.num_head])
                a1 = tf.reshape(a1, [self.batch_size, self.num_head, self.num_head, self.max_num_news])
                a1 = tf.transpose(a1, [0, 1, 2, 3])
                b1 = tf.tile(multiatt, [1, self.num_head, 1])
                b1 = tf.reshape(b1, [self.batch_size, self.num_head, self.num_head,self.max_num_news])
                self.att_loss += tf.norm(tf.reduce_sum(tf.multiply(a1, b1), axis=-1)-tf.eye(self.num_head, batch_shape=[self.batch_size]))
                ###
#                 self.variable_summary(multiatt, 'multiheadattention')
                multiatt = tf.expand_dims(multiatt, -1)
                o2 = tf.tile(multiatt, [1, 1, 1, self.linear_dim])
                o2 = tf.reshape(o2, [self.batch_size, self.num_head, self.max_num_news, self.linear_dim])
                ## [B, head, max_sequence, dim] --> repreat each att score dim times
                o3 = tf.reduce_sum(tf.multiply(o2, vs), axis=-2)
        
        with tf.variable_scope('static_attention'):
            att = tf.layers.dense(self.stock_embedding, self.num_head, name="static_attention")
            # att [BATCH_SIZE, NUM_HEAD], O3 [BATCH_SIZE, NUM_HEAD, DIM]
            att = tf.nn.softmax(att)
#             self.variable_summary(att, 'singleattention')
            att = tf.expand_dims(att, -1)
            att = tf.tile(att, [1, 1, self.linear_dim])
            output = tf.reduce_sum(tf.multiply(att, o3), axis=-2)
            output = tf.layers.dense(output, HIDDEN_SIZE, name="attention_output")
        if not self.is_training:
            return output
        return tf.nn.dropout(output, 1.0 - self.drop_out)
    

def run_epoch(session, merged, model, data, train_op, flag, output_log):
    total_costs = 0.0
    iters = 0
    cnt = 0
    all_acc = 0
    all_tn = all_tp = all_fp = all_fn = 0
    state = session.run(model.initial_state)
    for x, y, news, stockid in reader.news_iterator(data, model.batch_size, model.num_steps, model.max_num_news, flag):
#         print(x, y, news, stockid)
        cost, _, acc, summary, _, prediction = session.run(
                [model.cost, model.final_state, model.acc, merged, train_op, model.prediction],
                {model.input_data: x, model.targets: y, model.news: news, model.initial_state: state, model.stock_id:stockid})
        total_costs += cost
        iters += model.num_steps
        cnt += 1
        all_acc += acc
        
        tn, fp, fn, tp = confusion_matrix(y_true=y.reshape(-1), y_pred=prediction.reshape(-1)).ravel()
        all_tn += tn
        all_fp += fp
        all_fn += fn
        all_tp += tp
        mcc = (all_tp * all_tn - all_fp * all_fn) / math.sqrt((all_tp + all_fp) * (all_tp + all_fn) * (all_tn + all_fp) * (all_tn + all_fn))
        if output_log and iters % 1000 == 0:
            logger.info("After %d steps, cost is %.3f Acc is %.3f Mcc %.5f" % (iters, total_costs / iters, all_acc/cnt, mcc))  

    return total_costs/iters, all_acc/cnt, summary, mcc

# def tuning_parameter():
#     for max_num_news in [5, 10, 15]:
#         for lr in [0.001]:
#             for drop_out in [ 0.1, 0.3]:
#                 for batch_size in [4, 8, 16]:
#                     for num_head in [2, 3, 5]:
#                         for num_step in [5, 8, 10]:
#                             yield max_num_news, lr, batch_size, num_head, drop_out, num_step

def main(_):
    train_data, valid_data, test_data = reader.news_raw_data(DATA_PATH)
    
    max_num_news, lr, batch_size, num_head, drop_out, num_steps = 10, 0.001, 4, 3, 0.1, 5

    initializer = tf.random_uniform_initializer(-0.05, 0.05)
    tf.reset_default_graph()
    if os.path.exists(os.path.join(root_path, DATA_PATH, f"{info}train_preprocess.pkl")):
        os.remove(os.path.join(root_path, DATA_PATH, f'{info}train_preprocess.pkl'))
        os.remove(os.path.join(root_path, DATA_PATH, f'{info}valid_preprocess.pkl'))   
        os.remove(os.path.join(root_path, DATA_PATH, f'{info}test_preprocess.pkl'))
    with tf.variable_scope("StockMovementPrediction", reuse=None, initializer=initializer):
        train_model = StockMovementPrediction(True, batch_size, num_steps, LINEAR_DIM, num_head, drop_out, max_num_news, lr)

    saver = tf.train.Saver()
    writer = tf.summary.FileWriter("tensorboard/", tf.Session().graph)
    merged = tf.summary.merge_all()
    gpu_options = tf.GPUOptions(allow_growth=True)
    with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options)) as session:
        session.run(tf.initializers.global_variables())

        for i in range(NUM_EPOCH):
            logger.info("In iteration: %d" % (i+1))
            _, _, summary,_ = run_epoch(session, merged, train_model, train_data, train_model.train_op, 'train', True)
            writer.add_summary(summary, i)
            valid_cost, acc, _, mcc = run_epoch(session, merged, train_model, valid_data, tf.no_op(), 'valid', False)
            logger.info("Epoch: %d Validation Cost: %.3f, acc is %.3f, mcc is %.5f" % (i+1, valid_cost, acc, mcc))

        test_cost, acc, _, mcc = run_epoch(session, merged, train_model, test_data, tf.no_op(), 'test', False)
        logger.info("Test Cost: %.3f, acc is %.3f, mcc is %.5f" % (test_cost, acc, mcc))
        saver.save(session, f'model_saver/{info}model_btch{batch_size}_h{num_head}_d{drop_out}_step{num_steps}_news{max_num_news}_lr{lr}.ckpt')


if __name__ == "__main__":
    tf.app.run()
