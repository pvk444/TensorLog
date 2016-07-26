# (C) William W. Cohen and Carnegie Mellon University, 2016
#
# learning methods for Tensorlog
# 

import time
import math
import numpy as NP
import scipy.sparse as SS
import collections

import opfunutil
import funs
import tensorlog
import dataset
import mutil
import declare
import config

# clip to avoid exploding gradients

conf = config.Config()
conf.minGradient = -100;   conf.help.minGradient = "Clip gradients smaller than this to minGradient"
conf.maxGradient = +100;   conf.help.minGradient = "Clip gradients larger than this to maxGradient"

##############################################################################
# helper classes
##############################################################################

class GradAccumulator(object):
    """ Accumulate the sum gradients for perhaps many parameters, indexing
    them by parameter name.  Also maintains 'counter' statistics
    """
    def __init__(self):
        self.runningSum = {}
        self.counter = collections.defaultdict(float)
    def keys(self):
        return self.runningSum.keys()
    def items(self):
        return self.runningSum.items()
    def __getitem__(self,paramName):
        return self.runningSum[paramName]
    def __setitem__(self,paramName,gradient):
        self.runningSum[paramName] = gradient
    def accum(self,paramName,deltaGradient):
        """Increment the parameter with the given name by the appropriate
        amount."""
        mutil.checkCSR(deltaGradient,('deltaGradient for %s' % str(paramName)))
        if not paramName in self.runningSum:
            self.runningSum[paramName] = deltaGradient
        else:
            self.runningSum[paramName] = self.runningSum[paramName] + deltaGradient
            mutil.checkCSR(self.runningSum[paramName],('runningSum for %s' % str(paramName)))
    @staticmethod
    def counter():
        return collections.defaultdict(float)
    def mapData(self,mapFun):
        result = GradAccumulator()
        for k,m in self.items():
            result.accum(k, mutil.mapData(mapFun,m))
        return result
    def addedTo(self,other):
        result = GradAccumulator()
        for k,m in self.items():
            result.accum(k, m)
        for k,m in other.items():
            result.accum(k, m)
        return result
    @staticmethod
    def mergeCounters(gradAccums,initial=None):
        """Compute the min, max, total, avg, and weighted average of every
        counter, and return in a new defaultdict
        """ 
        ctr = initial if initial!=None else GradAccumulator.counter()
        keys = set()
        weightedTotalPrefix = '_wtot'
        #reduce with total,min,max, and weighted total
        for accum in gradAccums:
            ctr['counters'] += 1  # merged counters
            for k,v in accum.counter.items():
                keys.add(k)
                ctr[(k,'tot')] += v
                ctr[(k,weightedTotalPrefix)] += accum.counter['n']*v
                kmin = (k,'min')
                if kmin in ctr: ctr[kmin] = min(ctr[kmin],v)
                kmax = (k,'max')
                if kmin in ctr: ctr[kmin] = min(ctr[kmin],v)
        # convert weighted total to weighted avg
        totn = ctr[('n','tot')]
        for k in keys:
            ctr[(k,'avg')] += ctr[(k,weightedTotalPrefix)]/totn
            del ctr[(k,weightedTotalPrefix)]
        return ctr

class Tracer(object):

    """ Functions to pass in as arguments to a learner's "tracer"
    keyword argument.  These are called by the optimizer after
    gradient computation for each mode - at this point Y and P are
    known.
    """

    @staticmethod
    def silent(learner,gradAccum,Y,P,**kw):
        """No output."""
        gradAccum.counter['n'] = mutil.numRows(Y)
        pass

    @staticmethod
    def cheap(learner,gradAccum,Y,P,**kw):
        """Easy-to-compute status message."""
        gradAccum.counter['n'] = mutil.numRows(Y)
        Tracer._announce(gradAccum,
            Tracer.identification(learner,kw) 
            + Tracer.timing(learner,kw))
    
    @staticmethod
    def default(learner,gradAccum,Y,P,**kw):
        """A default status message."""
        gradAccum.counter['n'] = mutil.numRows(Y)
        Tracer._announce(gradAccum,
            Tracer.identification(learner,kw) 
            + Tracer.loss(learner,Y,P,kw) 
            + Tracer.timing(learner,kw))

    @staticmethod
    def recordDefaults(learner,gradAccum,Y,P,**kw):
        """A default status message."""
        gradAccum.counter['n'] = mutil.numRows(Y)
        Tracer._record(gradAccum,
            Tracer.identification(learner,kw) 
            + Tracer.loss(learner,Y,P,kw) 
            + Tracer.timing(learner,kw))

    @staticmethod
    def defaultPlusAcc(learner,gradAccum,Y,P,**kw):
        """A default status message."""
        gradAccum.counter['n'] = mutil.numRows(Y)
        Tracer._announce(gradAccum,
            Tracer.identification(learner,kw) 
            + Tracer.loss(learner,Y,P,kw) 
            + Tracer.accuracy(learner,Y,P,kw) 
            + Tracer.timing(learner,kw))

    @staticmethod
    def _announce(gradAccum,keyValuePairList):
        """ Print info in a list of key value pairs,
        and also store them in the gradAccum's counters.
        """
        pairs = Tracer._record(gradAccum,keyValuePairList)
        print ' '.join(pairs)

    @staticmethod
    def _record(gradAccum,keyValuePairList):
        """Prepare a printable list of key value pairs, and also store them
        in the gradAccum's counters.
        """
        pairs = []
        for (k,v) in keyValuePairList:
            gradAccum.counter[k] = v
            pairs.append(k)
            pairs.append('%g' % v)
        return pairs
        print ' '.join(pairs)


    #
    # return lists of key,value pairs that can be used in a status
    # message or counters, possibly making use of information from the
    # keywords
    # 

    @staticmethod
    def loss(learner,Y,P,kw):
        #perExample=False since we care about the sum xe+reg which is being optimized
        xe = learner.crossEntropy(Y,P,perExample=False)  
        reg = learner.regularizer.regularizationCost(learner.prog)
        return [('loss', (xe+reg)), ('crossEnt', xe), ('reg',reg)]

    @staticmethod
    def accuracy(learner,Y,P,kw):
        acc = learner.accuracy(Y,P)        
        return [('acc',acc)]

    @staticmethod
    def timing(learner,kw):
        """Return list of timing properties using keyword 'starttime'
        """
        return [('time',(time.time()-kw['startTime']))] if 'startTime' in kw else []

    @staticmethod
    def identification(learner,kw):
        """Return list of identifying properties taken from keywords and learner.
        Known keys are:
           i = current epoch
           k = current minibatch
           mode = current mode
        """
        result = []
        if 'k' in kw: result.append(('minibatch', kw['k']))
        if 'i' in kw: result.append(('epoch', kw['i']+1))
        if 'i' in kw: result.append(('maxEpoch',learner.epochs))
        if 'mode' in kw: result.append((('mode=%s' % (str(kw['mode']))), 1.0))
        return result

#TODO: rework to merge results

class EpochTracer(Tracer):

    """Functions to called by a learner after gradient computation for all
    modes and parameter updates.
    """
    defaultOutputs = [('crossEnt',['avg','tot']),('loss',['tot']),('reg',['avg']),
                      ('time',['min','avg','max','tot']),
                      ('n',['tot'])]

    @staticmethod
    def silent(learner,ctr,**kw):
        """No output."""
        pass

    @staticmethod
    def cheap(learner,ctr,**kw):
        """Easy-to-compute status message."""
        EpochTracer.default(learner,ctr,**kw)
    
    @staticmethod
    def default(learner,ctr,**kw):
        """A default status message."""
        pairs  = Tracer.identification(learner,kw)
        for k,prefs in EpochTracer.defaultOutputs:
            for pref in prefs:
                pairs.append( ((pref + '.' +k), ctr[(k,pref)]) )
        pairs.append(('minibatches',ctr['counters']))

        print ' '.join(map(lambda (k,v):('%s=%g'%(k,v)), pairs))


##############################################################################
# Learners
##############################################################################


class Learner(object):
    """Abstract class with some utility functions.."""

    # prog pts to db, rules
    def __init__(self,prog,regularizer,tracer,epochTracer):
        self.prog = prog
        self.regularizer = regularizer or NullRegularizer()
        self.tracer = tracer or Tracer.default
        self.epochTracer = epochTracer or EpochTracer.default

    #
    # using and measuring performance
    #

    def predict(self,mode,X,pad=None):
        """Make predictions on a data matrix associated with the given mode."""
        if not pad: pad = opfunutil.Scratchpad() 
        predictFun = self.prog.getPredictFunction(mode)
        result = predictFun.eval(self.prog.db, [X], pad)
        return result

    def datasetPredict(self,dset,copyXs=True):
        """ Return predictions on a dataset. """
        xDict = {}
        yDict = {}
        for mode in dset.modesToLearn():
            X = dset.getX(mode)
            xDict[mode] = X if copyXs else None
            try:
                #yDict[mode] = self.prog.getPredictFunction(mode).eval(self.prog.db, [X])
                yDict[mode] = self.predict(mode,X)
            except FloatingPointError:
                print "Trouble with mode %s" % str(mode)
                raise
        return dataset.Dataset(xDict,yDict)

    @staticmethod
    def datasetAccuracy(goldDset,predictedDset):
        """ Return accuracy on a dataset relative to gold labels. """
        weightedSum = 0.0
        totalWeight = 0.0
        for mode in goldDset.modesToLearn():
            assert predictedDset.hasMode(mode)
            Y = goldDset.getY(mode)
            P = predictedDset.getY(mode)
            weight = mutil.numRows(Y)
            weightedSum += weight * Learner.accuracy(Y,P)
            totalWeight += weight
        if totalWeight == 0: return 0
        return weightedSum/totalWeight

    @staticmethod
    def datasetCrossEntropy(goldDset,predictedDset,perExample=True):
        """ Return cross entropy on a dataset. """
        result = 0.0
        for mode in goldDset.modesToLearn():
            assert predictedDset.hasMode(mode)
            Y = goldDset.getY(mode)
            P = predictedDset.getY(mode)
            divisor = mutil.numRows(Y) if perExample else 1.0
            result += Learner.crossEntropy(Y,P,perExample=False)/divisor
        return result


    @staticmethod
    def accuracy(Y,P):
        """Evaluate accuracy of predictions P versus labels Y."""
        #TODO surely there's a better way of doing this
        def allZerosButArgmax(d):
            result = NP.zeros_like(d)
            result[d.argmax()] = 1.0
            return result
        n = mutil.numRows(P)
        ok = 0.0
        for i in range(n):
            pi = P.getrow(i)
            yi = Y.getrow(i)
            ti = mutil.mapData(allZerosButArgmax,pi)
            ok += yi.multiply(ti).sum()
        return ok/n

    @staticmethod
    def crossEntropy(Y,P,perExample=False):
        """Compute cross entropy some predications relative to some labels."""
        logP = mutil.mapData(NP.log,P)
        result = -(Y.multiply(logP).sum())
        return result/mutil.numRows(Y) if perExample else result

    def crossEntropyGrad(self,mode,X,Y,tracerArgs={},pad=None):
        """Compute the parameter gradient associated with softmax
        normalization followed by a cross-entropy cost function.  If a
        scratchpad is passed in, then intermediate results of the
        gradient computation will be saved on that scratchpad.
        """

        if not pad: pad = opfunutil.Scratchpad()

        # More detail: in learning we use a softmax normalization
        # followed immediately by a crossEntropy loss, which has a
        # simple derivative when combined - see
        # http://peterroelants.github.io/posts/neural_network_implementation_intermezzo02/
        # So in doing backprop, you don't call backprop on the outer
        # function, instead you compute the initial delta of P-Y, the
        # derivative for the loss of the (softmax o crossEntropy)
        # function, and it pass that delta down to the inner function
        # for softMax

        # do the prediction, saving intermediate outputs on the scratchpad
        predictFun = self.prog.getPredictFunction(mode)
        assert isinstance(predictFun,funs.SoftmaxFunction),'crossEntropyGrad specialized to work for softmax normalization'
        P = self.predict(mode,X,pad)

        # compute gradient
        paramGrads = GradAccumulator()
        #TODO assert rowSum(Y) = all ones - that's assumed here in
        #initial delta of Y-P
        predictFun.fun.backprop(Y-P,paramGrads,pad)

        # the tracer function may output status, and may also write
        # information to the counters in paramGrads
        self.tracer(self,paramGrads,Y,P,**tracerArgs)

        return paramGrads

    #
    # parameter update
    #

    def meanUpdate(self,functor,arity,delta,n,totalN=0):
        #clip the delta vector to avoid exploding gradients
        delta = mutil.mapData(lambda d:NP.clip(d,conf.minGradient,conf.maxGradient), delta)
        if arity==1:
            #for a parameter that is a row-vector, we have one
            #gradient per example and we will take the mean
            compensation = 1.0 if totalN==0 else float(n)/totalN
            return mutil.mean(delta)*compensation
        else:
            #for a parameter that is a matrix, we have one gradient for the whole matrix
            compensation = (1.0/n) if totalN==0 else (1.0/totalN)
            return delta*compensation
        

    def applyMeanUpdate(self,paramGrads,rate,n,totalN=0):
        """ Compute the mean of each parameter gradient, and add it to the
        appropriate param, after scaling by rate. If necessary clip
        negative parameters to zero.
        """ 

        for (functor,arity),delta0 in paramGrads.items():
            m0 = self.prog.db.getParameter(functor,arity)
            m1 = m0 + rate * self.meanUpdate(functor,arity,delta0,n,totalN=totalN)
            m = mutil.mapData(lambda d:NP.clip(d,0.0,NP.finfo('float64').max), m1)
            self.prog.db.setParameter(functor,arity,m)


#
# actual learner implementations
#

class OnePredFixedRateGDLearner(Learner):
    """ Simple one-predicate learner.
    """  
    def __init__(self,prog,epochs=10,rate=0.1,regularizer=None,tracer=None,epochTracer=None):
        super(OnePredFixedRateGDLearner,self).__init__(prog,regularizer=regularizer,tracer=tracer,epochTracer=epochTracer)
        self.epochs=epochs
        self.rate=rate
    
    def train(self,mode,X,Y):
        trainStartTime = time.time()
        for i in range(self.epochs):
            startTime = time.time()
            n = mutil.numRows(X)
            args = {'i':i,'startTime':startTime}
            paramGrads = self.crossEntropyGrad(mode,X,Y,tracerArgs=args)
            self.regularizer.regularizeParams(self.prog,n)
            self.applyMeanUpdate(paramGrads,self.rate,n)

class FixedRateGDLearner(Learner):
    """ A batch gradient descent learner.
    """

    def __init__(self,prog,epochs=10,rate=0.1,regularizer=None,tracer=None,epochTracer=None):
        super(FixedRateGDLearner,self).__init__(prog,regularizer=regularizer,tracer=tracer,epochTracer=epochTracer)
        self.epochs=epochs
        self.rate=rate
    
    def train(self,dset):
        trainStartTime = time.time()
        modes = dset.modesToLearn()
        numModes = len(modes)
        for i in range(self.epochs):
            startTime = time.time()
            epochCounter = GradAccumulator.counter()
            for j,mode in enumerate(dset.modesToLearn()):
                n = mutil.numRows(dset.getX(mode))
                args = {'i':i,'startTime':startTime,'mode':str(mode)}
                paramGrads = self.crossEntropyGrad(mode,dset.getX(mode),dset.getY(mode),tracerArgs=args)
                self.regularizer.regularizeParams(self.prog,n)
                self.applyMeanUpdate(paramGrads,self.rate,n)
                epochCounter = GradAccumulator.mergeCounters([paramGrads],epochCounter)
            self.epochTracer(self,epochCounter,i=i,startTime=trainStartTime)
            

class FixedRateSGDLearner(FixedRateGDLearner):

    """ A stochastic gradient descent learner.
    """

    def __init__(self,prog,epochs=10,rate=0.1,regularizer=None,tracer=None,miniBatchSize=100):
        super(FixedRateSGDLearner,self).__init__(
            prog,epochs=epochs,rate=rate,regularizer=regularizer,tracer=tracer)
        self.miniBatchSize = miniBatchSize
    
    def train(self,dset):
        trainStartTime = time.time()
        modes = dset.modesToLearn()
        n = len(modes)
        for i in range(self.epochs):
            startTime = time.time()
            epochCounter = GradAccumulator.counter()
            k = 0
            for (mode,X,Y) in dset.minibatchIterator(batchSize=self.miniBatchSize):
                n = mutil.numRows(X)
                k = k+1
                args = {'i':i,'k':k,'startTime':startTime,'mode':mode}
                paramGrads = self.crossEntropyGrad(mode,X,Y,tracerArgs=args)
                self.regularizer.regularizeParams(self.prog,n)
                self.applyMeanUpdate(paramGrads,self.rate,n)
                epochCounter = GradAccumulator.mergeCounters([paramGrads],epochCounter)

            self.epochTracer(self,epochCounter,i=i,startTime=trainStartTime)

##############################################################################
# regularizers
##############################################################################


class Regularizer(object):
    """Abstract class for regularizers."""

    def regularizeParams(self,prog,n):
        """Introduce the regularization gradient to a GradAccumulator."""
        assert False, 'abstract method called'

    def regularizationCost(self,prog):
        """Report the current regularization cost."""
        assert False, 'abstract method called'

class NullRegularizer(object):
    """ Default case which does no regularization"""

    def regularizeParams(self,prog,n):
        pass

    def regularizationCost(self,prog):
        return 0.0

class L2Regularizer(Regularizer):
    """ L2 regularization toward 0."""

    def __init__(self,regularizationConstant=0.01):
        self.regularizationConstant = regularizationConstant
    
    def regularizeParams(self,prog,n):
        for functor,arity in prog.db.params:
            m0 = prog.db.getParameter(functor,arity)
            m1 = m0 * (1.0 - self.regularizationConstant)
            prog.db.setParameter(functor,arity,m1)

    def regularizationCost(self,prog):
        result = 0
        for functor,arity in prog.db.params:
            m = prog.db.getParameter(functor,arity)
            result += (m.data * m.data).sum()
        return result*self.regularizationConstant

